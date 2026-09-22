"""A committed world and a wired payment module for application tests on PostgreSQL.

Use cases open their own transactions, so the rows they read must be committed: the
``app`` fixture creates fresh prefixed tables, seeds and commits them, and drops them after.

Tenant ``tenant-a`` has merchants ``m1`` and ``m2``; tenant ``tenant-b`` has ``mb``. Each
merchant has an active Test account bound to an active Test connection. ``m1`` also has a
Live account and connection, and a Test account ``m1_unbound`` that no connection binds.
Profile v1 is active with prefixes ``subscription/SUB``, ``topup/TOP`` and ``invoice/INV``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fakes.fake_provider import FakeProvider, StaticSecretResolver, body, signed_headers
from payment_module.adapters.sqlalchemy.tables import PaymentTables
from payment_module.adapters.sqlalchemy.uow import SqlAlchemyUnitOfWorkFactory
from payment_module.application.config import PaymentModuleConfig
from payment_module.application.create_intent import CreateIntentCommand, CreateIntentResult
from payment_module.application.ingest_webhook import IngestResult
from payment_module.builder import PaymentModule, build_payment_module
from payment_module.domain.enums import (
    AuthMode,
    ConnectionStatus,
    Environment,
    MerchantStatus,
    ProfileKind,
    ProfileStatus,
    ReceivingAccountStatus,
)
from payment_module.domain.reference import PaymentReference, ReferenceProfile
from payment_module.ports.handlers import SettlementView, TransactionOutcomeView
from payment_module.ports.publisher import OutboxEventView
from payment_module.ports.unit_of_work import UnitOfWork

START = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
SECRET = "whsec-test-1"
TENANT_A = "tenant-a"
TENANT_B = "tenant-b"
TEST = Environment.TEST
LIVE = Environment.LIVE


class FakeClock:
    def __init__(self, now: datetime = START) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current

    def advance(self, **delta: float) -> None:
        self.current += timedelta(**delta)


class RecordingHandler:
    """Records settlements; raises while ``failures`` is positive."""

    def __init__(self) -> None:
        self.calls: list[SettlementView] = []
        self.failures = 0

    async def on_settled(self, uow: UnitOfWork, settled: SettlementView) -> None:
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("host fulfilment failed")
        self.calls.append(settled)


class RecordingObserver:
    def __init__(self) -> None:
        self.outcomes: list[TransactionOutcomeView] = []

    async def on_outcome(self, uow: UnitOfWork, outcome: TransactionOutcomeView) -> None:
        self.outcomes.append(outcome)


class RecordingMetrics:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.tags: list[tuple[str, dict[str, str]]] = []

    def increment(self, name: str, tags: Any = None) -> None:
        self.counts[name] = self.counts.get(name, 0) + 1
        self.tags.append((name, dict(tags or {})))


class RecordingPublisher:
    """Counts deliveries per event id; raises while ``failures`` is positive."""

    def __init__(self) -> None:
        self.delivered: list[OutboxEventView] = []
        self.failures = 0

    async def publish(self, event: OutboxEventView) -> None:
        if self.failures > 0:
            self.failures -= 1
            raise ConnectionError("broker unavailable")
        self.delivered.append(event)


@dataclass
class Scope:
    tenant_id: str
    merchant_id: uuid.UUID
    account_id: uuid.UUID
    connection_id: uuid.UUID
    locator: str
    account_number: str
    environment: Environment


@dataclass
class App:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    tables: PaymentTables
    clock: FakeClock
    provider: FakeProvider
    secrets: StaticSecretResolver
    handler: RecordingHandler
    observer: RecordingObserver
    metrics: RecordingMetrics
    publisher: RecordingPublisher
    module: PaymentModule
    m1: Scope
    m1_live: Scope
    m2: Scope
    mb: Scope
    m1_unbound_account: str
    _ids: int = field(default=1000)

    def uow_factory(self) -> SqlAlchemyUnitOfWorkFactory:
        return SqlAlchemyUnitOfWorkFactory(self.sessions, self.tables)

    def build(self, **overrides: Any) -> PaymentModule:
        """Another module on the same database, e.g. with a different config or policy."""
        values: dict[str, Any] = {
            "config": PaymentModuleConfig(worker_owner="worker-test"),
            "uow_factory": self.uow_factory(),
            "provider_registry": {"fake": self.provider},
            "secret_resolver": self.secrets,
            "clock": self.clock,
            "settlement_handler": self.handler,
            "outcome_observer": self.observer,
            "metrics": self.metrics,
            "outbox_publisher": self.publisher,
        }
        return build_payment_module(**(values | overrides))

    def next_tx_id(self) -> int:
        self._ids += 1
        return self._ids

    def command(self, scope: Scope | None = None, **over: Any) -> CreateIntentCommand:
        scope = scope or self.m1
        values: dict[str, Any] = {
            "tenant_id": scope.tenant_id,
            "merchant_id": scope.merchant_id,
            "receiving_account_id": scope.account_id,
            "amount_vnd": 150_000,
            "host_ref_type": "order",
            "host_ref_id": f"order-{uuid.uuid4().hex[:8]}",
            "idempotency_key": f"idem-{uuid.uuid4().hex[:8]}",
            "expires_at": self.clock.now() + timedelta(minutes=15),
            "prefix_name": "subscription",
        }
        return CreateIntentCommand(**(values | over))

    async def intent(self, scope: Scope | None = None, **over: Any) -> CreateIntentResult:
        return await self.module.create_intent.execute(self.command(scope, **over))

    def payment(self, scope: Scope | None = None, **over: Any) -> bytes:
        """Body of a payment into ``scope``'s account (``m1`` by default)."""
        scope = scope or self.m1
        values: dict[str, Any] = {"account": scope.account_number}
        if "tx_id" not in over:
            values["tx_id"] = self.next_tx_id()
        values |= over
        tx_id = values.pop("tx_id")
        return body(tx_id, **values)

    def headers(self, raw: bytes, *, secret: str = SECRET, at: datetime | None = None) -> dict:
        return signed_headers(raw, secret, int((at or self.clock.now()).timestamp()))

    async def webhook(self, raw: bytes, scope: Scope | None = None) -> IngestResult:
        scope = scope or self.m1
        return await self.module.ingest_webhook.execute(scope.locator, raw, self.headers(raw))

    async def pay(self, scope: Scope | None = None, **over: Any) -> IngestResult:
        """Ingest a payment into ``scope`` and run the worker once."""
        result = await self.webhook(self.payment(scope, **over), scope)
        await self.module.process_inbox.run_batch()
        return result

    async def rows(self, table: sa.Table, *where: Any) -> list[sa.Row]:
        async with self.engine.connect() as conn:
            return list((await conn.execute(sa.select(table).where(*where))).all())

    async def count(self, table: sa.Table, *where: Any) -> int:
        async with self.engine.connect() as conn:
            query = sa.select(sa.func.count()).select_from(table).where(*where)
            return (await conn.execute(query)).scalar_one()

    async def execute(self, statement: Any) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(statement)

    async def set_connection_status(self, scope: Scope, status: ConnectionStatus) -> None:
        t = self.tables.provider_connections
        await self.execute(
            sa.update(t).where(t.c.id == scope.connection_id).values(status=status.value)
        )


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


async def seed_world(conn: Any, t: PaymentTables) -> dict[str, Any]:
    await conn.execute(
        t.reference_profiles.insert().values(
            version=1,
            kind=ProfileKind.GENERATED.value,
            suffix_length=6,
            alphabet="ABCDEFGHJKLMNPQRSTUVWXYZ23456789",
            status=ProfileStatus.ACTIVE.value,
        )
    )
    await conn.execute(
        t.reference_profile_prefixes.insert(),
        [
            {"profile_version": 1, "name": "subscription", "prefix": "SUB"},
            {"profile_version": 1, "name": "topup", "prefix": "TOP"},
            {"profile_version": 1, "name": "invoice", "prefix": "INV"},
        ],
    )
    numbers = iter(range(1000001, 1000100))

    async def merchant(tenant_id: str) -> uuid.UUID:
        merchant_id = _uuid()
        await conn.execute(
            t.merchants.insert().values(
                id=merchant_id,
                tenant_id=tenant_id,
                host_merchant_ref=f"shop-{merchant_id.hex[:6]}",
                status=MerchantStatus.ACTIVE.value,
            )
        )
        return merchant_id

    async def account(tenant_id: str, merchant_id: uuid.UUID, env: Environment) -> tuple:
        account_id, number = _uuid(), str(next(numbers))
        await conn.execute(
            t.receiving_accounts.insert().values(
                id=account_id,
                tenant_id=tenant_id,
                merchant_id=merchant_id,
                environment=env.value,
                bank_code="VCB",
                account_number=number,
                account_number_masked=f"****{number[-4:]}",
                holder_name="CONG TY A",
                account_fingerprint=f"VCB|{number}|",
                status=ReceivingAccountStatus.ACTIVE.value,
            )
        )
        return account_id, number

    async def scope(tenant_id: str, merchant_id: uuid.UUID, env: Environment) -> Scope:
        account_id, number = await account(tenant_id, merchant_id, env)
        connection_id, locator = _uuid(), uuid.uuid4().hex
        await conn.execute(
            t.provider_connections.insert().values(
                id=connection_id,
                tenant_id=tenant_id,
                merchant_id=merchant_id,
                provider="fake",
                environment=env.value,
                locator=locator,
                secret_ref="env:FAKE_WEBHOOK_SECRET",
                auth_mode=AuthMode.HMAC.value,
                status=ConnectionStatus.ACTIVE.value,
            )
        )
        await conn.execute(
            t.connection_account_bindings.insert().values(
                tenant_id=tenant_id,
                merchant_id=merchant_id,
                environment=env.value,
                connection_id=connection_id,
                receiving_account_id=account_id,
                created_by="test",
            )
        )
        return Scope(tenant_id, merchant_id, account_id, connection_id, locator, number, env)

    m1, m2, mb = await merchant(TENANT_A), await merchant(TENANT_A), await merchant(TENANT_B)
    scopes = {
        "m1": await scope(TENANT_A, m1, TEST),
        "m1_live": await scope(TENANT_A, m1, LIVE),
        "m2": await scope(TENANT_A, m2, TEST),
        "mb": await scope(TENANT_B, mb, TEST),
    }
    _, unbound_number = await account(TENANT_A, m1, TEST)
    return scopes | {"m1_unbound_account": unbound_number}


class ScriptedReferences:
    """A reference generator that returns the given codes first, then fresh ones."""

    def __init__(self, codes: list[str]) -> None:
        self.codes = list(codes)

    def generate(self, profile: ReferenceProfile, prefix_name: str) -> PaymentReference:
        if self.codes:
            return PaymentReference(self.codes.pop(0))
        suffix = uuid.uuid4().hex[:6].upper()
        return PaymentReference(f"{profile.prefix_for(prefix_name)}{suffix}")


class RaisingMetrics(RecordingMetrics):
    """A host metrics backend that fails on every call, after recording it."""

    def increment(self, name: str, tags: Any = None) -> None:
        super().increment(name, tags)
        raise OSError("metrics backend unavailable")
