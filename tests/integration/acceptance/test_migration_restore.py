"""Case "Migration/restore": a database migrated with the frozen ``schema_v1``, holding
money, survives ``pg_dump -Fc`` / ``pg_restore`` with every constraint still enforced and
every count and amount unchanged. Two settled transfers of the same amount and bank
reference stay two facts."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from fakes.payment_app import START, seed_world
from payment_module.adapters.sqlalchemy.migrations import schema_v1
from payment_module.adapters.sqlalchemy.tables import PaymentTables, define_tables
from payment_module.domain.enums import (
    FirstSource,
    IdentityKind,
    IntentStatus,
    MatchState,
    SettlementOrigin,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.acceptance,
    pytest.mark.timeout(300),
]

MONEY_TABLES = ("payment_intents", "provider_transactions", "settlements")
# CHECK text is re-deparsed by pg_restore, so only keys and foreign keys compare by
# definition; the CHECKs are proven by the rejected inserts after the restore.
CONSTRAINTS = sa.text(
    "SELECT conrelid::regclass::text, conname, contype, "
    "CASE WHEN contype = 'c' THEN '' ELSE pg_get_constraintdef(oid) END "
    "FROM pg_constraint WHERE connamespace = 'public'::regnamespace"
)
NULLABILITY = sa.text(
    "SELECT table_name, column_name, is_nullable, data_type FROM information_schema.columns "
    "WHERE table_schema = 'public'"
)


def _migrate(sync_conn: sa.Connection) -> None:
    schema_v1.upgrade(Operations(MigrationContext.configure(sync_conn)))


async def _settled_pair(conn: AsyncConnection, t: PaymentTables, scope: Any, n: int) -> dict:
    """An intent paid by an incoming fact; returns the settlement row."""
    intent_id, tx_id = uuid.uuid4(), uuid.uuid4()
    common = {"tenant_id": scope.tenant_id, "environment": scope.environment.value}
    await conn.execute(
        t.payment_intents.insert().values(
            id=intent_id,
            merchant_id=scope.merchant_id,
            receiving_account_id=scope.account_id,
            amount_vnd=150_000,
            beneficiary_snapshot={"bank_code": "VCB"},
            payment_reference=f"SUB{n:06d}",
            reference_profile_version=1,
            reference_prefix_name="subscription",
            host_ref_type="order",
            host_ref_id=f"order-{n}",
            idempotency_key=f"idem-{n}",
            request_fingerprint="fp",
            expires_at=START + timedelta(minutes=15),
            status=IntentStatus.PAID.value,
            **common,
        )
    )
    account_key = f"VCB|{scope.account_number}|"
    await conn.execute(
        t.provider_transactions.insert().values(
            id=tx_id,
            merchant_id=scope.merchant_id,
            provider="fake",
            provider_account_key=account_key,
            receiving_account_id=scope.account_id,
            identity_kind=IdentityKind.WEBHOOK_ID.value,
            identity_value=str(n),
            dedup_key=f"fake|{account_key}|webhook_id|{n}",
            webhook_tx_id=str(n),
            bank_reference="FT-SAME",
            amount_vnd=150_000,
            direction="in",
            first_source=FirstSource.WEBHOOK.value,
            match_state=MatchState.SETTLED.value,
            **common,
        )
    )
    settlement = {
        "id": uuid.uuid4(),
        "transaction_id": tx_id,
        "intent_id": intent_id,
        "receiving_account_id": scope.account_id,
        "amount_vnd": 150_000,
        "intent_amount_vnd": 150_000,
        "origin": SettlementOrigin.AUTO.value,
        "settled_at": START,
        **common,
    }
    await conn.execute(t.settlements.insert().values(**settlement))
    return settlement


async def _snapshot(url: str, metadata: sa.MetaData) -> dict[str, Any]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            counts = {
                table.name: (
                    await conn.execute(sa.select(sa.func.count()).select_from(table))
                ).scalar_one()
                for table in metadata.sorted_tables
            }
            sums = {
                name: (
                    await conn.execute(sa.select(sa.func.sum(metadata.tables[name].c.amount_vnd)))
                ).scalar_one()
                for name in (f"pm_{table}" for table in MONEY_TABLES)
            }
            constraints = set((await conn.execute(CONSTRAINTS)).tuples())
            columns = set((await conn.execute(NULLABILITY)).tuples())
    finally:
        await engine.dispose()
    return {"counts": counts, "sums": sums, "constraints": constraints, "columns": columns}


def _run(container: Any, *command: str) -> None:
    result = container.exec(list(command))
    assert result.exit_code == 0, result.output.decode(errors="replace")


async def _violation(conn: AsyncConnection, table: sa.Table, values: dict) -> str | None:
    with pytest.raises(IntegrityError) as caught:
        async with conn.begin_nested():
            await conn.execute(table.insert().values(**values))
    cause = caught.value.orig.__cause__ if caught.value.orig is not None else None
    return getattr(cause, "constraint_name", None)


async def test_dump_and_restore_keeps_money_and_constraints(pg_container, create_database) -> None:
    if pg_container is None:
        pytest.skip("pg_dump/pg_restore run inside the test container; DATABASE_URL is external")
    metadata = sa.MetaData()
    t = define_tables(metadata)
    source_url = await create_database("restore_src")
    target_url = await create_database("restore_dst")
    source = create_async_engine(source_url)
    try:
        async with source.begin() as conn:
            await conn.run_sync(_migrate)
            scopes = await seed_world(conn, t)
            settlements = [await _settled_pair(conn, t, scopes["m1"], n) for n in (1, 2)]
    finally:
        await source.dispose()

    user = pg_container.username
    dump = f"/tmp/{make_url(source_url).database}.dump"
    _run(pg_container, "pg_dump", "-U", user, "-Fc", "-f", dump, make_url(source_url).database)
    _run(
        pg_container,
        "pg_restore",
        "-U",
        user,
        "--exit-on-error",
        "-d",
        make_url(target_url).database,
        dump,
    )

    before = await _snapshot(source_url, metadata)
    after = await _snapshot(target_url, metadata)
    assert after == before
    assert after["sums"]["pm_settlements"] == 300_000
    assert after["counts"]["pm_provider_transactions"] == 2

    target = create_async_engine(target_url)
    try:
        async with target.connect() as conn:
            await conn.begin()
            again = settlements[0] | {"id": uuid.uuid4()}
            short = again | {"amount_vnd": 140_000}
            dangling = again | {
                "tenant_id": scopes["mb"].tenant_id,
                "transaction_id": uuid.uuid4(),
                "intent_id": uuid.uuid4(),
            }
            assert await _violation(conn, t.settlements, short) == "pm_settlements_exact_amount_ck"
            assert await _violation(conn, t.settlements, again) == "pm_settlements_intent_uq"
            assert await _violation(conn, t.settlements, dangling) in {
                "pm_settlements_transaction_fk",
                "pm_settlements_intent_fk",
            }
            await conn.rollback()
    finally:
        await target.dispose()
