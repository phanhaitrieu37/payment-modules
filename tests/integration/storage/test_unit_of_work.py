"""Unit of work and repositories on PostgreSQL: owned and host-joined transactions,
intent creation with idempotency and reference retry, fact dedup, and round trips."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from payment_module.adapters.sqlalchemy.tables import PaymentTables
from payment_module.adapters.sqlalchemy.uow import (
    SqlAlchemyUnitOfWork,
    SqlAlchemyUnitOfWorkFactory,
)
from payment_module.domain.enums import (
    ConnectionStatus,
    Direction,
    Environment,
    FirstSource,
    IdentityKind,
    IntentStatus,
    MatchState,
    MerchantStatus,
    ObservationSource,
    ProfileKind,
    ProfileStatus,
    ReadinessStatus,
    ReconcileMode,
    ReviewReason,
    SettlementOrigin,
)
from payment_module.domain.money import AmountVnd
from payment_module.domain.reference import NamedPrefix, ReferenceProfile
from payment_module.ports import unit_of_work as port
from payment_module.ports.unit_of_work import (
    IdempotencyConflict,
    NewPaymentIntent,
    NewProviderTransaction,
    ReferenceSpaceExhausted,
)

pytestmark = [pytest.mark.postgres, pytest.mark.timeout(600)]

NOW = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
TEST = Environment.TEST
LIVE = Environment.LIVE


@dataclass
class Setup:
    sessions: async_sessionmaker[AsyncSession]
    tables: PaymentTables
    tenant_id: str
    merchant_id: uuid.UUID
    account_id: uuid.UUID
    live_account_id: uuid.UUID
    connection_id: uuid.UUID
    account_key: str

    def uow(self) -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(self.sessions, self.tables)

    def new_intent(self, **over: object) -> NewPaymentIntent:
        values: dict[str, object] = {
            "tenant_id": self.tenant_id,
            "merchant_id": self.merchant_id,
            "environment": TEST,
            "receiving_account_id": self.account_id,
            "amount": AmountVnd(150_000),
            "beneficiary_snapshot": {"bank_code": "VCB", "account_name": "CONG TY A"},
            "reference_profile_version": 1,
            "reference_prefix_name": "subscription",
            "host_ref_type": "order",
            "host_ref_id": "order-1",
            "idempotency_key": "idem-1",
            "request_fingerprint": "fp-1",
            "expires_at": NOW + timedelta(minutes=15),
        }
        return NewPaymentIntent(**(values | over))  # type: ignore[arg-type]


def references(*codes: str) -> Callable[[], str]:
    iterator: Iterator[str] = iter(codes)
    return lambda: next(iterator)


@pytest.fixture
async def setup(engine: AsyncEngine, tables: PaymentTables) -> AsyncIterator[Setup]:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with SqlAlchemyUnitOfWork(sessions, tables) as uow:
        await uow.reference_profiles.add(
            ReferenceProfile(
                version=1,
                prefixes=(NamedPrefix("subscription", "SUB"), NamedPrefix("topup", "TOP")),
                suffix_length=6,
                alphabet="ABCDEFGHJKLMNPQRSTUVWXYZ23456789",
                kind=ProfileKind.GENERATED,
                status=ProfileStatus.ACTIVE,
            )
        )
        merchant_id = await uow.merchants.add("tenant-a", "shop-1")
        account = {
            "tenant_id": "tenant-a",
            "merchant_id": merchant_id,
            "bank_code": "VCB",
            "bank_bin": "970436",
            "account_number": "0071000123456",
            "sub_account": None,
            "account_number_masked": "*********3456",
            "holder_name": "CONG TY A",
            "account_fingerprint": "VCB|0071000123456|",
        }
        account_id = await uow.receiving_accounts.add(environment=TEST, **account)
        live_account_id = await uow.receiving_accounts.add(environment=LIVE, **account)
        connection_id = await uow.connections.add(
            tenant_id="tenant-a",
            merchant_id=merchant_id,
            provider="sepay",
            environment=TEST,
            locator=uuid.uuid4().hex,
            secret_ref="env:SEPAY_WEBHOOK_SECRET",
        )
        await uow.commit()
    yield Setup(
        sessions,
        tables,
        "tenant-a",
        merchant_id,
        account_id,
        live_account_id,
        connection_id,
        "VCB|0071000123456|",
    )


async def _intent_count(setup: Setup) -> int:
    async with setup.sessions() as session:
        return await session.scalar(
            sa.select(sa.func.count()).select_from(setup.tables.payment_intents)
        )


def test_adapter_implements_every_port_repository_method() -> None:
    annotations = inspect.get_annotations(port.UnitOfWork, eval_str=True)
    for attribute, protocol in annotations.items():
        methods = [
            name
            for name, member in vars(protocol).items()
            if inspect.isfunction(member) and not name.startswith("_")
        ]
        repository_type = inspect.get_annotations(SqlAlchemyUnitOfWork, eval_str=True)[attribute]
        for name in methods:
            assert hasattr(repository_type, name), f"{attribute}.{name} is not implemented"
    for name in ("commit", "rollback"):
        assert hasattr(SqlAlchemyUnitOfWork, name)


# Owned unit of work.


async def test_leaving_without_commit_rolls_back(setup: Setup) -> None:
    async with setup.uow() as uow:
        await uow.intents.create(setup.new_intent(), references("SUBAAAAAA"))
    assert await _intent_count(setup) == 0


async def test_commit_persists_and_factory_builds_fresh_units(setup: Setup) -> None:
    factory = SqlAlchemyUnitOfWorkFactory(setup.sessions, setup.tables)
    async with factory() as uow:
        intent, created = await uow.intents.create(setup.new_intent(), references("SUBAAAAAA"))
        await uow.commit()
    assert created
    async with factory() as uow:
        found = await uow.intents.get_by_idempotency_key(setup.tenant_id, TEST, "idem-1")
    assert found == intent
    assert found.status == IntentStatus.AWAITING_PAYMENT
    assert found.payment_reference == "SUBAAAAAA"


# Host-joined unit of work.


async def test_joined_commit_only_flushes_and_host_rollback_discards_intent(
    setup: Setup,
) -> None:
    async with setup.sessions() as host:
        async with SqlAlchemyUnitOfWork.joined(host, setup.tables) as uow:
            assert uow.session is host
            await uow.intents.create(setup.new_intent(), references("SUBAAAAAA"))
            await uow.commit()
        assert host.in_transaction()
        await host.rollback()
    assert await _intent_count(setup) == 0


async def test_joined_intent_commits_with_host_transaction(setup: Setup) -> None:
    async with setup.sessions() as host:
        async with SqlAlchemyUnitOfWork.joined(host, setup.tables) as uow:
            await uow.intents.create(setup.new_intent(), references("SUBAAAAAA"))
            await uow.commit()
        await host.commit()
    assert await _intent_count(setup) == 1


async def test_joined_rollback_undoes_package_writes_but_keeps_host_transaction(
    setup: Setup,
) -> None:
    merchants = setup.tables.merchants
    async with setup.sessions() as host:
        await host.execute(
            merchants.insert().values(
                id=uuid.uuid4(),
                tenant_id="tenant-a",
                host_merchant_ref="host-row",
                status=MerchantStatus.ACTIVE.value,
            )
        )
        async with SqlAlchemyUnitOfWork.joined(host, setup.tables) as uow:
            await uow.intents.create(setup.new_intent(), references("SUBAAAAAA"))
            await uow.rollback()
        await host.commit()
    assert await _intent_count(setup) == 0
    async with setup.sessions() as session:
        refs = await session.scalars(sa.select(merchants.c.host_merchant_ref))
        assert "host-row" in set(refs)


async def test_reference_collision_retries_inside_host_transaction(setup: Setup) -> None:
    async with setup.uow() as uow:
        await uow.intents.create(
            setup.new_intent(idempotency_key="other", host_ref_id="order-0"),
            references("SUBAAAAAA"),
        )
        await uow.commit()
    async with setup.sessions() as host:
        async with SqlAlchemyUnitOfWork.joined(host, setup.tables) as uow:
            intent, created = await uow.intents.create(
                setup.new_intent(), references("SUBAAAAAA", "SUBBBBBBB")
            )
            await uow.commit()
        # The collision never aborted the host transaction: it can keep working and commit.
        assert await host.scalar(sa.select(sa.literal(1))) == 1
        await host.commit()
    assert created
    assert intent.payment_reference == "SUBBBBBBB"
    assert await _intent_count(setup) == 2


async def test_same_idempotency_key_and_request_returns_existing_intent(setup: Setup) -> None:
    async with setup.sessions() as host:
        async with SqlAlchemyUnitOfWork.joined(host, setup.tables) as uow:
            first, first_created = await uow.intents.create(
                setup.new_intent(), references("SUBAAAAAA")
            )
            again, again_created = await uow.intents.create(
                setup.new_intent(), references("SUBCCCCCC")
            )
            await uow.commit()
        await host.commit()
    assert (first_created, again_created) == (True, False)
    assert again == first


async def test_same_idempotency_key_other_request_conflicts(setup: Setup) -> None:
    async with setup.sessions() as host:
        async with SqlAlchemyUnitOfWork.joined(host, setup.tables) as uow:
            await uow.intents.create(setup.new_intent(), references("SUBAAAAAA"))
            with pytest.raises(IdempotencyConflict):
                await uow.intents.create(
                    setup.new_intent(request_fingerprint="fp-2", amount=AmountVnd(1)),
                    references("SUBCCCCCC"),
                )
            await uow.commit()
        await host.commit()
    assert await _intent_count(setup) == 1


async def test_reference_space_exhausted_after_bounded_retries(setup: Setup) -> None:
    async with setup.uow() as uow:
        await uow.intents.create(setup.new_intent(idempotency_key="other"), references("SUBAAAAAA"))
        calls = 0

        def always_same() -> str:
            nonlocal calls
            calls += 1
            return "SUBAAAAAA"

        with pytest.raises(ReferenceSpaceExhausted):
            await uow.intents.create(setup.new_intent(), always_same)
    assert calls == 5


async def test_constraint_errors_are_not_swallowed_and_host_can_continue(
    setup: Setup,
) -> None:
    async with setup.sessions() as host:
        with pytest.raises(IntegrityError):
            async with SqlAlchemyUnitOfWork.joined(host, setup.tables) as uow:
                await uow.intents.create(
                    setup.new_intent(reference_prefix_name="unknown"), references("SUBAAAAAA")
                )
        assert await host.scalar(sa.select(sa.literal(1))) == 1
        await host.commit()
    assert await _intent_count(setup) == 0


async def test_concurrent_create_with_same_key_returns_the_winner(setup: Setup) -> None:
    async with setup.uow() as first:
        winner, created = await first.intents.create(setup.new_intent(), references("SUBAAAAAA"))
        assert created

        async def second_request() -> tuple[object, bool]:
            async with setup.uow() as second:
                result = await second.intents.create(setup.new_intent(), references("SUBCCCCCC"))
                await second.commit()
                return result

        racing = asyncio.create_task(second_request())
        await asyncio.sleep(0.5)
        assert not racing.done(), "the second insert should wait for the first transaction"
        await first.commit()
    loser, loser_created = await asyncio.wait_for(racing, timeout=10)
    assert loser_created is False
    assert loser == winner


# Intent lookups.


async def test_reference_lookup_is_project_wide_locked_and_ordered(setup: Setup) -> None:
    async with setup.uow() as uow:
        tenant_b_merchant = await uow.merchants.add("tenant-b", "shop-b")
        account_b = await uow.receiving_accounts.add(
            tenant_id="tenant-b",
            merchant_id=tenant_b_merchant,
            environment=TEST,
            bank_code="ACB",
            bank_bin=None,
            account_number="123",
            sub_account="VA1",
            account_number_masked="*23",
            holder_name="B",
            account_fingerprint="ACB|123|VA1",
        )
        a, _ = await uow.intents.create(setup.new_intent(), references("SUBAAAAAA"))
        b, _ = await uow.intents.create(
            setup.new_intent(
                tenant_id="tenant-b",
                merchant_id=tenant_b_merchant,
                receiving_account_id=account_b,
                idempotency_key="idem-b",
            ),
            references("SUBBBBBBB"),
        )
        found = await uow.intents.find_by_references_for_update(
            ["SUBAAAAAA", "SUBBBBBBB", "SUBZZZZZZ"]
        )
        assert [intent.id for intent in found] == sorted([a.id, b.id])
        assert {intent.tenant_id for intent in found} == {"tenant-a", "tenant-b"}
        assert await uow.intents.find_by_references_for_update([]) == []
        assert await uow.intents.get_for_update("tenant-a", a.id) == a
        assert await uow.intents.get_for_update("tenant-b", a.id) is None


# Canonical facts.


def _fact(setup: Setup, **over: object) -> NewProviderTransaction:
    values: dict[str, object] = {
        "tenant_id": setup.tenant_id,
        "environment": TEST,
        "provider": "sepay",
        "provider_account_key": setup.account_key,
        "dedup_key": f"sepay|{setup.account_key}|webhook_id|123",
        "identity_kind": IdentityKind.WEBHOOK_ID,
        "identity_value": "123",
        "receiving_account_id": setup.account_id,
        "merchant_id": setup.merchant_id,
        "amount": AmountVnd(150_000),
        "direction": Direction.IN,
        "bank_reference": "FT26265000001",
        "first_source": FirstSource.WEBHOOK,
    }
    return NewProviderTransaction(**(values | over))  # type: ignore[arg-type]


async def test_insert_or_get_by_dedup_key(setup: Setup) -> None:
    async with setup.uow() as uow:
        first, created = await uow.transactions.insert_or_get_by_dedup_key(_fact(setup))
        again, again_created = await uow.transactions.insert_or_get_by_dedup_key(_fact(setup))
        live, live_created = await uow.transactions.insert_or_get_by_dedup_key(
            _fact(setup, environment=LIVE, receiving_account_id=setup.live_account_id)
        )
        api, api_created = await uow.transactions.insert_or_get_by_dedup_key(
            _fact(
                setup,
                dedup_key=f"sepay|{setup.account_key}|api_id|uuid-1",
                identity_kind=IdentityKind.API_ID,
                identity_value="uuid-1",
                first_source=FirstSource.RECONCILE,
            )
        )
        await uow.commit()
    assert (created, again_created, live_created, api_created) == (True, False, True, True)
    assert again == first
    assert live.id != first.id
    t = setup.tables.provider_transactions
    async with setup.sessions() as session:
        rows = {
            row.id: row
            for row in await session.execute(
                sa.select(t.c.id, t.c.webhook_tx_id, t.c.api_tx_id, t.c.match_state)
            )
        }
    assert (rows[first.id].webhook_tx_id, rows[first.id].api_tx_id) == ("123", None)
    assert (rows[api.id].webhook_tx_id, rows[api.id].api_tx_id) == (None, "uuid-1")
    assert rows[first.id].match_state == MatchState.RECORDED.value


async def test_other_unique_conflicts_are_not_treated_as_dedup(setup: Setup) -> None:
    async with setup.uow() as uow:
        await uow.transactions.insert_or_get_by_dedup_key(_fact(setup))
        with pytest.raises(IntegrityError):
            # Same webhook id in the same scope under a different dedup key.
            await uow.transactions.insert_or_get_by_dedup_key(_fact(setup, dedup_key="other"))


# Round trips of the remaining repositories.


async def test_repository_round_trips(setup: Setup) -> None:
    async with setup.uow() as uow:
        profile = await uow.reference_profiles.get(1)
        assert profile is not None
        assert [p.prefix for p in profile.prefixes] == ["SUB", "TOP"]
        assert await uow.reference_profiles.get(2) is None

        assert await uow.merchants.get_id_by_host_ref("tenant-a", "shop-1") == setup.merchant_id
        account = await uow.receiving_accounts.get(setup.tenant_id, setup.account_id)
        assert account is not None
        assert (account.sub_account, account.account_name) == (None, "CONG TY A")
        by_fingerprint = await uow.receiving_accounts.find_by_fingerprint(LIVE, setup.account_key)
        assert by_fingerprint is not None and by_fingerprint.id == setup.live_account_id

        connection = await uow.connections.get(setup.tenant_id, setup.connection_id)
        assert connection is not None
        assert connection.status == ConnectionStatus.PENDING
        assert connection.reconcile_mode == ReconcileMode.DETECT_ONLY
        assert connection.timestamp_tolerance_seconds == 300
        assert await uow.connections.by_locator(connection.locator) == connection
        assert await uow.connections.get("tenant-b", setup.connection_id) is None

        await uow.connection_bindings.add(
            tenant_id=setup.tenant_id,
            merchant_id=setup.merchant_id,
            environment=TEST,
            connection_id=setup.connection_id,
            receiving_account_id=setup.account_id,
            created_by="operator-1",
        )
        assert await uow.connection_bindings.account_ids(setup.connection_id) == [setup.account_id]

        await uow.readiness.add(
            tenant_id=setup.tenant_id,
            connection_id=setup.connection_id,
            profile_version=1,
            environment=TEST,
            checklist={"items": []},
        )
        status = await uow.readiness.get_status(setup.connection_id, 1, TEST)
        assert status == ReadinessStatus.PENDING

        intent, _ = await uow.intents.create(setup.new_intent(), references("SUBAAAAAA"))
        fact, _ = await uow.transactions.insert_or_get_by_dedup_key(_fact(setup))
        assert await uow.transactions.get(setup.tenant_id, TEST, fact.id) == fact

        run_id = await uow.reconciliation_runs.start(
            tenant_id=setup.tenant_id,
            connection_id=setup.connection_id,
            window_from=NOW - timedelta(hours=1),
            window_to=NOW,
            started_at=NOW,
        )
        await uow.observations.add(
            tenant_id=setup.tenant_id,
            environment=TEST,
            connection_id=setup.connection_id,
            provider="sepay",
            source=ObservationSource.API,
            source_tx_id="uuid-1",
            reconciliation_run_id=run_id,
            reported_account_key=setup.account_key,
            amount=AmountVnd(150_000),
            direction=Direction.IN,
            observed_at=NOW,
            transaction_id=fact.id,
        )
        case_id = await uow.review_cases.open(
            tenant_id=setup.tenant_id,
            environment=TEST,
            transaction_id=fact.id,
            reason=ReviewReason.LATE,
            details={"note": "late"},
            opened_at=NOW,
            candidate_intent_id=intent.id,
        )
        settlement_id = await uow.settlements.add(
            tenant_id=setup.tenant_id,
            environment=TEST,
            transaction_id=fact.id,
            intent_id=intent.id,
            receiving_account_id=setup.account_id,
            amount=fact.amount,
            intent_amount=intent.amount,
            origin=SettlementOrigin.OPERATOR_REVIEW,
            settled_at=NOW,
            review_case_id=case_id,
            resolved_by="operator-1",
        )
        await uow.commit()
    assert isinstance(settlement_id, uuid.UUID)
