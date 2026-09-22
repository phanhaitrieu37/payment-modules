"""CreateIntent: readiness gate, named prefixes, idempotency, reference retry, legacy import,
joined host transactions; and GetIntentStatus."""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import timedelta

import pytest
import sqlalchemy as sa

from fakes.payment_app import LIVE, App, ScriptedReferences
from payment_module.adapters.sqlalchemy.uow import SqlAlchemyUnitOfWork
from payment_module.domain.enums import (
    ConnectionStatus,
    IntentStatus,
    MerchantStatus,
    ProfileKind,
    ProfileStatus,
    ReceivingAccountStatus,
)
from payment_module.domain.errors import (
    IdempotencyConflict,
    IntentNotFound,
    IntentRejected,
    ReferenceSpaceExhausted,
)

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


async def intents(app: App) -> int:
    return await app.count(app.tables.payment_intents)


async def test_creates_intent_with_instruction_and_snapshot(app: App) -> None:
    result = await app.intent(amount_vnd=250_000)

    assert result.created
    assert result.payment_reference.startswith("SUB")
    assert len(result.payment_reference) == 3 + 6
    assert result.instruction.payment_reference == result.payment_reference
    assert result.instruction.amount.value == 250_000
    assert result.instruction.account_number == app.m1.account_number
    [row] = await app.rows(app.tables.payment_intents)
    assert row.status == IntentStatus.AWAITING_PAYMENT.value
    assert row.environment == app.m1.environment.value
    assert row.reference_profile_version == 1
    assert row.reference_prefix_name == "subscription"
    assert row.beneficiary_snapshot["account_number"] == app.m1.account_number


async def test_environment_comes_from_the_account(app: App) -> None:
    result = await app.intent(app.m1_live)
    assert result.intent.environment == LIVE


@pytest.mark.parametrize(
    ("name", "prefix"), [("subscription", "SUB"), ("topup", "TOP"), ("invoice", "INV")]
)
async def test_each_named_prefix_generates_its_own_codes(app: App, name: str, prefix: str) -> None:
    result = await app.intent(prefix_name=name)
    assert result.payment_reference.startswith(prefix)
    [row] = await app.rows(app.tables.payment_intents)
    assert row.reference_prefix_name == name


@pytest.mark.parametrize("name", ["unknown", None])
async def test_unknown_or_missing_prefix_name_is_rejected(app: App, name: str | None) -> None:
    with pytest.raises(IntentRejected) as caught:
        await app.intent(prefix_name=name)
    assert caught.value.code == "UNKNOWN_REFERENCE_PREFIX"
    assert await intents(app) == 0


async def test_same_request_is_idempotent(app: App) -> None:
    command = app.command()
    first = await app.module.create_intent.execute(command)
    again = await app.module.create_intent.execute(command)

    assert not again.created
    assert again.intent.id == first.intent.id
    assert again.payment_reference == first.payment_reference
    assert await intents(app) == 1


async def test_same_key_with_another_request_conflicts(app: App) -> None:
    command = app.command()
    await app.module.create_intent.execute(command)
    with pytest.raises(IdempotencyConflict):
        await app.module.create_intent.execute(dataclasses.replace(command, amount_vnd=99_000))
    assert await intents(app) == 1


@pytest.mark.parametrize(
    "status", [ConnectionStatus.PENDING, ConnectionStatus.NOT_READY, ConnectionStatus.DISABLED]
)
async def test_connection_not_active_rejects(app: App, status: ConnectionStatus) -> None:
    await app.set_connection_status(app.m1, status)
    with pytest.raises(IntentRejected) as caught:
        await app.intent()
    assert caught.value.code == "RECEIVING_ACCOUNT_NOT_READY"
    assert await intents(app) == 0


async def test_account_without_any_bound_connection_is_rejected(app: App) -> None:
    [row] = await app.rows(
        app.tables.receiving_accounts,
        app.tables.receiving_accounts.c.account_number == app.m1_unbound_account,
    )
    with pytest.raises(IntentRejected) as caught:
        await app.intent(receiving_account_id=row.id)
    assert caught.value.code == "RECEIVING_ACCOUNT_NOT_READY"


@pytest.mark.parametrize(
    "status", [ReceivingAccountStatus.DISABLED, ReceivingAccountStatus.RETIRED]
)
async def test_inactive_account_is_rejected(app: App, status: ReceivingAccountStatus) -> None:
    t = app.tables.receiving_accounts
    await app.execute(sa.update(t).where(t.c.id == app.m1.account_id).values(status=status.value))
    with pytest.raises(IntentRejected) as caught:
        await app.intent()
    assert caught.value.code == "RECEIVING_ACCOUNT_NOT_READY"


async def test_disabled_merchant_is_rejected(app: App) -> None:
    t = app.tables.merchants
    await app.execute(
        sa.update(t)
        .where(t.c.id == app.m1.merchant_id)
        .values(status=MerchantStatus.DISABLED.value)
    )
    with pytest.raises(IntentRejected) as caught:
        await app.intent()
    assert caught.value.code == "MERCHANT_INACTIVE"


async def test_account_of_another_merchant_is_rejected(app: App) -> None:
    with pytest.raises(IntentRejected) as caught:
        await app.intent(receiving_account_id=app.m2.account_id)
    assert caught.value.code == "RECEIVING_ACCOUNT_NOT_READY"


async def test_merchant_of_another_tenant_is_rejected(app: App) -> None:
    with pytest.raises(IntentRejected) as caught:
        await app.intent(
            tenant_id=app.m1.tenant_id,
            merchant_id=app.mb.merchant_id,
            receiving_account_id=app.mb.account_id,
        )
    assert caught.value.code == "MERCHANT_INACTIVE"


async def test_no_active_profile_is_rejected(app: App) -> None:
    t = app.tables.reference_profiles
    await app.execute(
        sa.update(t).where(t.c.version == 1).values(status=ProfileStatus.ACCEPTED_LEGACY.value)
    )
    with pytest.raises(IntentRejected) as caught:
        await app.intent()
    assert caught.value.code == "RECEIVING_ACCOUNT_NOT_READY"


@pytest.mark.parametrize(
    ("amount", "code"), [(0, "AMOUNT_NOT_POSITIVE"), (-5, "AMOUNT_NOT_POSITIVE")]
)
async def test_amount_must_be_positive(app: App, amount: int, code: str) -> None:
    with pytest.raises(IntentRejected) as caught:
        await app.intent(amount_vnd=amount)
    assert caught.value.code == code


async def test_expiry_must_be_in_the_future(app: App) -> None:
    with pytest.raises(IntentRejected) as caught:
        await app.intent(expires_at=app.clock.now())
    assert caught.value.code == "EXPIRES_IN_PAST"


async def test_normal_path_does_not_take_a_profile_version(app: App) -> None:
    with pytest.raises(IntentRejected) as caught:
        await app.intent(reference_profile_version=1)
    assert caught.value.code == "PROFILE_VERSION_NOT_ALLOWED"


async def test_reference_collisions_are_retried(app: App) -> None:
    taken = (await app.intent()).payment_reference
    module = app.build(reference_generator=ScriptedReferences([taken] * 4))
    result = await module.create_intent.execute(app.command())
    assert result.created and result.payment_reference != taken
    assert await intents(app) == 2


async def test_reference_collisions_give_up_after_five_attempts(app: App) -> None:
    taken = (await app.intent()).payment_reference
    module = app.build(reference_generator=ScriptedReferences([taken] * 5))
    with pytest.raises(ReferenceSpaceExhausted):
        await module.create_intent.execute(app.command())
    assert await intents(app) == 1


async def add_legacy_profile(app: App, version: int = 9) -> int:
    await app.execute(
        app.tables.reference_profiles.insert().values(
            version=version,
            kind=ProfileKind.LEGACY_IMPORT.value,
            status=ProfileStatus.ACCEPTED_LEGACY.value,
        )
    )
    return version


async def test_legacy_reference_is_imported(app: App) -> None:
    version = await add_legacy_profile(app)
    result = await app.intent(
        prefix_name=None, reference_override=" old-2024-77 ", reference_profile_version=version
    )
    assert result.payment_reference == "OLD-2024-77"
    [row] = await app.rows(app.tables.payment_intents)
    assert row.reference_profile_version == version
    assert row.reference_prefix_name is None


async def test_legacy_reference_already_used_is_rejected(app: App) -> None:
    version = await add_legacy_profile(app)
    await app.intent(prefix_name=None, reference_override="OLD1", reference_profile_version=version)
    with pytest.raises(IntentRejected) as caught:
        await app.intent(
            prefix_name=None, reference_override="old1", reference_profile_version=version
        )
    assert caught.value.code == "REFERENCE_ALREADY_USED"
    assert await intents(app) == 1


async def test_legacy_reference_with_prefix_name_is_rejected(app: App) -> None:
    version = await add_legacy_profile(app)
    with pytest.raises(IntentRejected) as caught:
        await app.intent(reference_override="OLD1", reference_profile_version=version)
    assert caught.value.code == "REFERENCE_OVERRIDE_WITH_PREFIX"


@pytest.mark.parametrize("version", [None, 1, 42], ids=["missing", "generated", "unknown"])
async def test_legacy_reference_needs_a_legacy_profile(app: App, version: int | None) -> None:
    with pytest.raises(IntentRejected) as caught:
        await app.intent(
            prefix_name=None, reference_override="OLD1", reference_profile_version=version
        )
    assert caught.value.code == "LEGACY_PROFILE_REQUIRED"
    assert await intents(app) == 0


@pytest.mark.parametrize("override", ["two tokens", "x" * 65])
async def test_legacy_reference_must_be_one_short_token(app: App, override: str) -> None:
    version = await add_legacy_profile(app)
    with pytest.raises(IntentRejected) as caught:
        await app.intent(
            prefix_name=None, reference_override=override, reference_profile_version=version
        )
    assert caught.value.code == "INVALID_REFERENCE"


async def test_joined_create_commits_with_the_host(app: App) -> None:
    async with app.sessions() as session, session.begin():
        uow = SqlAlchemyUnitOfWork.joined(session, app.tables)
        result = await app.module.create_intent.execute(app.command(), uow=uow)
        assert result.created
        assert await intents(app) == 0  # not visible before the host commits
    assert await intents(app) == 1


async def test_joined_rejection_leaves_the_host_transaction_usable(app: App) -> None:
    async with app.sessions() as session, session.begin():
        uow = SqlAlchemyUnitOfWork.joined(session, app.tables)
        with pytest.raises(IntentRejected):
            await app.module.create_intent.execute(app.command(prefix_name="nope"), uow=uow)
        assert (await session.execute(sa.text("SELECT 1"))).scalar_one() == 1
        await app.module.create_intent.execute(app.command(), uow=uow)
    assert await intents(app) == 1


class _HostFailed(Exception):
    pass


async def test_joined_host_rollback_discards_the_intent(app: App) -> None:
    with pytest.raises(_HostFailed):
        async with app.sessions() as session, session.begin():
            uow = SqlAlchemyUnitOfWork.joined(session, app.tables)
            await app.module.create_intent.execute(app.command(), uow=uow)
            raise _HostFailed
    assert await intents(app) == 0


async def _joined_create(app: App, command, hold: asyncio.Event | None = None):
    async with app.sessions() as session, session.begin():
        uow = SqlAlchemyUnitOfWork.joined(session, app.tables)
        try:
            result = await app.module.create_intent.execute(command, uow=uow)
        except IdempotencyConflict as exc:
            outcome: object = exc
        else:
            outcome = result
        # The host keeps using its transaction after the call, conflict or not.
        assert (await session.execute(sa.text("SELECT 1"))).scalar_one() == 1
        if hold is not None:
            await hold.wait()
        return outcome


@pytest.mark.timeout(600)
async def test_concurrent_same_request_in_host_transactions_yields_one_intent(app: App) -> None:
    command = app.command()
    release = asyncio.Event()
    first = asyncio.create_task(_joined_create(app, command, hold=release))
    await asyncio.sleep(0.2)
    second = asyncio.create_task(_joined_create(app, command))
    await asyncio.sleep(0.2)
    release.set()
    results = await asyncio.gather(first, second)

    assert all(not isinstance(item, Exception) for item in results)
    assert {item.intent.id for item in results} == {results[0].intent.id}
    assert sorted(item.created for item in results) == [False, True]
    assert await intents(app) == 1


@pytest.mark.timeout(600)
async def test_concurrent_different_request_same_key_one_wins_one_conflicts(app: App) -> None:
    command = app.command()
    release = asyncio.Event()
    first = asyncio.create_task(_joined_create(app, command, hold=release))
    await asyncio.sleep(0.2)
    second = asyncio.create_task(
        _joined_create(app, dataclasses.replace(command, amount_vnd=99_000))
    )
    await asyncio.sleep(0.2)
    release.set()
    winner, loser = await asyncio.gather(first, second)

    assert not isinstance(winner, Exception) and winner.created
    assert isinstance(loser, IdempotencyConflict)
    assert await intents(app) == 1


async def test_status_reports_expired_without_writing(app: App) -> None:
    created = await app.intent(expires_at=app.clock.now() + timedelta(minutes=1))
    view = await app.module.get_intent_status.execute(app.m1.tenant_id, created.intent.id)
    assert view.effective_status == IntentStatus.AWAITING_PAYMENT

    app.clock.advance(minutes=2)
    view = await app.module.get_intent_status.execute(app.m1.tenant_id, created.intent.id)
    assert view.effective_status == IntentStatus.EXPIRED
    assert view.intent.status == IntentStatus.AWAITING_PAYMENT
    [row] = await app.rows(app.tables.payment_intents)
    assert row.status == IntentStatus.AWAITING_PAYMENT.value


async def test_status_of_another_tenant_is_not_found(app: App) -> None:
    created = await app.intent()
    with pytest.raises(IntentNotFound):
        await app.module.get_intent_status.execute(app.mb.tenant_id, created.intent.id)


async def test_joined_reference_collision_retries_without_aborting_the_host(app: App) -> None:
    taken = (await app.intent()).payment_reference
    module = app.build(reference_generator=ScriptedReferences([taken, taken]))
    async with app.sessions() as session, session.begin():
        uow = SqlAlchemyUnitOfWork.joined(session, app.tables)
        result = await module.create_intent.execute(app.command(), uow=uow)
        assert result.created and result.payment_reference != taken
        assert (await session.execute(sa.text("SELECT 1"))).scalar_one() == 1
    assert await intents(app) == 2
