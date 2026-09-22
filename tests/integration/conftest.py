"""PostgreSQL fixtures for integration tests.

``pg_url`` starts one disposable ``postgres:17`` container per test session (testcontainers,
random host port). Set ``DATABASE_URL`` to an async SQLAlchemy URL to use an external server
instead. Every test gets its own table prefix, so tests never share rows and a test that
commits cannot leak into another one.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from payment_module.adapters.sqlalchemy.tables import PaymentTables, define_tables
from payment_module.domain.enums import (
    AuthMode,
    ConnectionStatus,
    Direction,
    Environment,
    EventKeyKind,
    FirstSource,
    IdentityKind,
    InboxStatus,
    IntentStatus,
    LinkStatus,
    MatchState,
    MerchantStatus,
    ObservationSource,
    ProfileKind,
    ProfileStatus,
    ReadinessStatus,
    ReceivingAccountStatus,
    ReconciliationRunStatus,
    ReviewCaseStatus,
    ReviewReason,
    SettlementOrigin,
)

NOW = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Fail collection when a test under ``tests/integration`` lacks the ``integration``
    marker, so the CI selection ``-m integration`` can never silently skip it."""
    here = Path(__file__).parent
    unmarked = [
        item.nodeid
        for item in items
        if here in item.path.parents and item.get_closest_marker("integration") is None
    ]
    if unmarked:
        raise pytest.UsageError(f"integration tests without the integration marker: {unmarked}")


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    external = os.environ.get("DATABASE_URL")
    if external:
        yield external
        return
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer("postgres:17", driver=None) as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(5432)
        yield (
            f"postgresql+asyncpg://{container.username}:{container.password}"
            f"@{host}:{port}/{container.dbname}"
        )


@pytest.fixture
async def engine(pg_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(pg_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def unique_prefix() -> str:
    return f"t{uuid.uuid4().hex[:8]}_"


@pytest.fixture
async def tables(engine: AsyncEngine) -> AsyncIterator[PaymentTables]:
    metadata = sa.MetaData()
    payment_tables = define_tables(metadata, prefix=unique_prefix())
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    try:
        yield payment_tables
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(metadata.drop_all)


def violated_constraint(error: IntegrityError) -> str | None:
    """Name of the constraint PostgreSQL reported, without the table prefix."""
    cause = error.orig.__cause__ if error.orig is not None else None
    name = getattr(cause, "constraint_name", None)
    return name.split("_", 1)[1] if name else None


def _id() -> uuid.UUID:
    return uuid.uuid4()


@dataclass
class Seed:
    """Inserts rows with direct SQL Core, filling valid defaults for every NOT NULL column.

    Every insert runs in its own savepoint, so an expected ``IntegrityError`` leaves the
    outer transaction usable for the next assertion.
    """

    conn: AsyncConnection
    t: PaymentTables
    _counter: int = field(default=0)

    def _next(self) -> int:
        self._counter += 1
        return self._counter

    async def insert(self, table: sa.Table, values: dict[str, Any]) -> dict[str, Any]:
        async with self.conn.begin_nested():
            await self.conn.execute(table.insert().values(**values))
        return values

    async def expect_violation(self, constraint: str, table: sa.Table, **values: Any) -> None:
        with pytest.raises(IntegrityError) as caught:
            await self.insert(table, values)
        assert violated_constraint(caught.value) == constraint

    async def expect_not_null(self, column: str, table: sa.Table, **values: Any) -> None:
        with pytest.raises(IntegrityError) as caught:
            await self.insert(table, values)
        cause = caught.value.orig.__cause__ if caught.value.orig is not None else None
        assert getattr(cause, "column_name", None) == column

    # Row builders: return the full value dict so callers can reuse or override columns.

    def merchant_row(self, tenant_id: str, **over: Any) -> dict[str, Any]:
        return {
            "id": _id(),
            "tenant_id": tenant_id,
            "host_merchant_ref": f"merchant-{self._next()}",
            "status": MerchantStatus.ACTIVE.value,
        } | over

    async def merchant(self, tenant_id: str, **over: Any) -> uuid.UUID:
        return (await self.insert(self.t.merchants, self.merchant_row(tenant_id, **over)))["id"]

    def account_row(
        self, tenant_id: str, merchant_id: uuid.UUID, environment: Environment, **over: Any
    ) -> dict[str, Any]:
        number = f"{1000000 + self._next()}"
        return {
            "id": _id(),
            "tenant_id": tenant_id,
            "merchant_id": merchant_id,
            "environment": environment.value,
            "bank_code": "VCB",
            "account_number": number,
            "account_number_masked": f"****{number[-4:]}",
            "holder_name": "CONG TY A",
            "account_fingerprint": f"VCB|{number}|",
            "status": ReceivingAccountStatus.ACTIVE.value,
        } | over

    async def account(
        self, tenant_id: str, merchant_id: uuid.UUID, environment: Environment, **over: Any
    ) -> uuid.UUID:
        row = self.account_row(tenant_id, merchant_id, environment, **over)
        return (await self.insert(self.t.receiving_accounts, row))["id"]

    def connection_row(
        self, tenant_id: str, merchant_id: uuid.UUID, environment: Environment, **over: Any
    ) -> dict[str, Any]:
        return {
            "id": _id(),
            "tenant_id": tenant_id,
            "merchant_id": merchant_id,
            "provider": "sepay",
            "environment": environment.value,
            "locator": uuid.uuid4().hex,
            "secret_ref": "env:SEPAY_WEBHOOK_SECRET",
            "auth_mode": AuthMode.HMAC.value,
            "status": ConnectionStatus.ACTIVE.value,
        } | over

    async def connection(
        self, tenant_id: str, merchant_id: uuid.UUID, environment: Environment, **over: Any
    ) -> uuid.UUID:
        row = self.connection_row(tenant_id, merchant_id, environment, **over)
        return (await self.insert(self.t.provider_connections, row))["id"]

    async def profile(
        self, version: int, status: ProfileStatus = ProfileStatus.DRAFT, **over: Any
    ) -> int:
        row = {
            "version": version,
            "kind": ProfileKind.GENERATED.value,
            "suffix_length": 6,
            "alphabet": "ABCDEFGHJKLMNPQRSTUVWXYZ23456789",
            "status": status.value,
        } | over
        await self.insert(self.t.reference_profiles, row)
        return version

    async def prefix(self, version: int, name: str, prefix: str) -> None:
        await self.insert(
            self.t.reference_profile_prefixes,
            {"profile_version": version, "name": name, "prefix": prefix},
        )

    def readiness_row(
        self, tenant_id: str, connection_id: uuid.UUID, environment: Environment, version: int
    ) -> dict[str, Any]:
        return {
            "id": _id(),
            "tenant_id": tenant_id,
            "connection_id": connection_id,
            "profile_version": version,
            "environment": environment.value,
            "status": ReadinessStatus.PENDING.value,
            "checklist": {"items": []},
        }

    def intent_row(
        self,
        tenant_id: str,
        merchant_id: uuid.UUID,
        environment: Environment,
        account_id: uuid.UUID,
        *,
        amount: int = 150_000,
        version: int = 1,
        prefix_name: str | None = "subscription",
        **over: Any,
    ) -> dict[str, Any]:
        number = self._next()
        return {
            "id": _id(),
            "tenant_id": tenant_id,
            "merchant_id": merchant_id,
            "environment": environment.value,
            "receiving_account_id": account_id,
            "amount_vnd": amount,
            "beneficiary_snapshot": {"bank_code": "VCB"},
            "payment_reference": f"SUB{uuid.uuid4().hex[:8].upper()}",
            "reference_profile_version": version,
            "reference_prefix_name": prefix_name,
            "host_ref_type": "order",
            "host_ref_id": f"order-{number}",
            "idempotency_key": f"idem-{number}",
            "request_fingerprint": "fp",
            "expires_at": NOW + timedelta(minutes=15),
            "status": IntentStatus.AWAITING_PAYMENT.value,
        } | over

    async def intent(self, *args: Any, **kwargs: Any) -> uuid.UUID:
        return (await self.insert(self.t.payment_intents, self.intent_row(*args, **kwargs)))["id"]

    def inbox_row(self, tenant_id: str, connection_id: uuid.UUID, **over: Any) -> dict[str, Any]:
        number = self._next()
        return {
            "id": _id(),
            "tenant_id": tenant_id,
            "connection_id": connection_id,
            "event_key": f"webhook:{number}",
            "event_key_kind": EventKeyKind.PROVIDER_ID.value,
            "body_sha256": "0" * 64,
            "raw_body": b"{}",
            "received_at": NOW,
            "status": InboxStatus.RECEIVED.value,
        } | over

    async def inbox(self, tenant_id: str, connection_id: uuid.UUID, **over: Any) -> uuid.UUID:
        row = self.inbox_row(tenant_id, connection_id, **over)
        return (await self.insert(self.t.webhook_inbox, row))["id"]

    async def run(self, tenant_id: str, connection_id: uuid.UUID) -> uuid.UUID:
        row = {
            "id": _id(),
            "tenant_id": tenant_id,
            "connection_id": connection_id,
            "window_from": NOW - timedelta(hours=1),
            "window_to": NOW,
            "status": ReconciliationRunStatus.RUNNING.value,
            "counts": {},
            "started_at": NOW,
        }
        return (await self.insert(self.t.reconciliation_runs, row))["id"]

    def transaction_row(
        self,
        tenant_id: str,
        environment: Environment,
        *,
        account_key: str = "VCB|1000001|",
        merchant_id: uuid.UUID | None = None,
        account_id: uuid.UUID | None = None,
        amount: int = 150_000,
        direction: Direction = Direction.IN,
        **over: Any,
    ) -> dict[str, Any]:
        webhook_tx_id = str(self._next())
        return {
            "id": _id(),
            "tenant_id": tenant_id,
            "merchant_id": merchant_id,
            "environment": environment.value,
            "provider": "sepay",
            "provider_account_key": account_key,
            "receiving_account_id": account_id,
            "identity_kind": IdentityKind.WEBHOOK_ID.value,
            "identity_value": webhook_tx_id,
            "dedup_key": f"sepay|{account_key}|webhook_id|{webhook_tx_id}",
            "webhook_tx_id": webhook_tx_id,
            "amount_vnd": amount,
            "direction": direction.value,
            "first_source": FirstSource.WEBHOOK.value,
            "match_state": MatchState.RECORDED.value,
        } | over

    async def transaction(self, *args: Any, **kwargs: Any) -> uuid.UUID:
        row = self.transaction_row(*args, **kwargs)
        return (await self.insert(self.t.provider_transactions, row))["id"]

    def observation_row(
        self,
        tenant_id: str,
        environment: Environment,
        connection_id: uuid.UUID,
        *,
        inbox_id: uuid.UUID | None = None,
        run_id: uuid.UUID | None = None,
        account_key: str = "VCB|1000001|",
        **over: Any,
    ) -> dict[str, Any]:
        source = ObservationSource.WEBHOOK if run_id is None else ObservationSource.API
        return {
            "id": _id(),
            "tenant_id": tenant_id,
            "environment": environment.value,
            "connection_id": connection_id,
            "provider": "sepay",
            "source": source.value,
            "source_tx_id": str(self._next()),
            "inbox_id": inbox_id,
            "reconciliation_run_id": run_id,
            "reported_account_key": account_key,
            "amount_vnd": 150_000,
            "direction": Direction.IN.value,
            "link_status": LinkStatus.UNLINKED.value,
            "observed_at": NOW,
        } | over

    def review_row(
        self, tenant_id: str, environment: Environment, transaction_id: uuid.UUID, **over: Any
    ) -> dict[str, Any]:
        return {
            "id": _id(),
            "tenant_id": tenant_id,
            "environment": environment.value,
            "transaction_id": transaction_id,
            "reason": ReviewReason.AMOUNT_MISMATCH.value,
            "details": {},
            "status": ReviewCaseStatus.OPEN.value,
            "opened_at": NOW,
        } | over

    async def review(self, *args: Any, **kwargs: Any) -> uuid.UUID:
        return (await self.insert(self.t.review_cases, self.review_row(*args, **kwargs)))["id"]

    def settlement_row(
        self,
        tenant_id: str,
        environment: Environment,
        *,
        transaction_id: uuid.UUID,
        intent_id: uuid.UUID,
        account_id: uuid.UUID,
        amount: int = 150_000,
        intent_amount: int = 150_000,
        **over: Any,
    ) -> dict[str, Any]:
        return {
            "id": _id(),
            "tenant_id": tenant_id,
            "environment": environment.value,
            "transaction_id": transaction_id,
            "intent_id": intent_id,
            "receiving_account_id": account_id,
            "amount_vnd": amount,
            "intent_amount_vnd": intent_amount,
            "origin": SettlementOrigin.AUTO.value,
            "settled_at": NOW,
        } | over


@pytest.fixture
async def seed(engine: AsyncEngine, tables: PaymentTables) -> AsyncIterator[Seed]:
    async with engine.connect() as conn:
        await conn.begin()
        try:
            yield Seed(conn, tables)
        finally:
            await conn.rollback()


@dataclass
class World:
    """Two tenants; tenant A has two merchants. Accounts and connections exist in both
    environments, and profile version 1 is active with the ``subscription`` prefix."""

    a: str
    b: str
    m1: uuid.UUID
    m2: uuid.UUID
    mb: uuid.UUID
    acc: dict[tuple[uuid.UUID, Environment], uuid.UUID]
    conn: dict[tuple[uuid.UUID, Environment], uuid.UUID]
    key: dict[uuid.UUID, str]


@pytest.fixture
async def world(seed: Seed) -> World:
    await seed.profile(1, ProfileStatus.ACTIVE)
    await seed.prefix(1, "subscription", "SUB")
    a, b = "tenant-a", "tenant-b"
    m1, m2, mb = await seed.merchant(a), await seed.merchant(a), await seed.merchant(b)
    owners = {m1: a, m2: a, mb: b}
    acc: dict[tuple[uuid.UUID, Environment], uuid.UUID] = {}
    conn: dict[tuple[uuid.UUID, Environment], uuid.UUID] = {}
    key: dict[uuid.UUID, str] = {}
    for merchant_id, tenant_id in owners.items():
        for environment in Environment:
            row = seed.account_row(tenant_id, merchant_id, environment)
            await seed.insert(seed.t.receiving_accounts, row)
            acc[merchant_id, environment] = row["id"]
            key[row["id"]] = row["account_fingerprint"]
            conn[merchant_id, environment] = await seed.connection(
                tenant_id, merchant_id, environment
            )
    return World(a=a, b=b, m1=m1, m2=m2, mb=mb, acc=acc, conn=conn, key=key)
