"""``define_tables`` + ``create_all`` and the frozen ``schema_v1.upgrade`` build the same schema.

Both run in one PostgreSQL database under different prefixes; the reflected DDL (tables,
columns with type/nullability/default, primary keys, unique constraints, CHECK text, foreign
keys with their targets, and indexes including partial ``WHERE`` clauses) must be equal once
the prefixes are removed.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.ext.asyncio import AsyncEngine

from payment_module.adapters.sqlalchemy.migrations import schema_v1
from payment_module.adapters.sqlalchemy.tables import TABLE_COUNT, define_tables

pytestmark = [pytest.mark.integration, pytest.mark.postgres, pytest.mark.timeout(600)]


def unique_prefix() -> str:
    return f"t{uuid.uuid4().hex[:8]}_"


def _strip(value: Any, prefix: str) -> Any:
    if isinstance(value, str):
        return value.replace(prefix, "")
    if isinstance(value, tuple):
        return tuple(_strip(item, prefix) for item in value)
    if isinstance(value, list):
        return [_strip(item, prefix) for item in value]
    if isinstance(value, dict):
        return {_strip(key, prefix): _strip(item, prefix) for key, item in value.items()}
    return value


def _snapshot(sync_conn: sa.Connection, prefix: str) -> dict[str, Any]:
    inspector = sa.inspect(sync_conn)
    snapshot: dict[str, Any] = {}
    for table in sorted(t for t in inspector.get_table_names() if t.startswith(prefix)):
        columns = [
            {
                "name": column["name"],
                # compile() keeps "WITH TIME ZONE"; str() renders both variants as TIMESTAMP.
                "type": column["type"].compile(dialect=sync_conn.dialect),
                "nullable": column["nullable"],
                "default": column["default"],
            }
            for column in inspector.get_columns(table)
        ]
        primary_key = inspector.get_pk_constraint(table)
        uniques = sorted(
            (u["name"], tuple(u["column_names"])) for u in inspector.get_unique_constraints(table)
        )
        checks = sorted((c["name"], c["sqltext"]) for c in inspector.get_check_constraints(table))
        foreign_keys = sorted(
            (
                fk["name"],
                tuple(fk["constrained_columns"]),
                fk["referred_table"],
                tuple(fk["referred_columns"]),
                tuple(sorted(fk["options"].items())),
            )
            for fk in inspector.get_foreign_keys(table)
        )
        indexes = sorted(
            (
                ix["name"],
                tuple(ix["column_names"]),
                bool(ix["unique"]),
                ix.get("dialect_options", {}).get("postgresql_where"),
            )
            for ix in inspector.get_indexes(table)
        )
        snapshot[table] = {
            "columns": columns,
            "primary_key": (primary_key["name"], tuple(primary_key["constrained_columns"])),
            "uniques": uniques,
            "checks": checks,
            "foreign_keys": foreign_keys,
            "indexes": indexes,
        }
    return _strip(snapshot, prefix)


def _upgrade(sync_conn: sa.Connection, prefix: str) -> None:
    schema_v1.upgrade(Operations(MigrationContext.configure(sync_conn)), prefix=prefix)


def _downgrade(sync_conn: sa.Connection, prefix: str) -> None:
    schema_v1.downgrade(Operations(MigrationContext.configure(sync_conn)), prefix=prefix)


@pytest.fixture
async def both(engine: AsyncEngine) -> AsyncIterator[tuple[dict[str, Any], dict[str, Any]]]:
    live_prefix, frozen_prefix = unique_prefix(), unique_prefix()
    metadata = sa.MetaData()
    define_tables(metadata, prefix=live_prefix)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        await conn.run_sync(_upgrade, frozen_prefix)
    try:
        async with engine.connect() as conn:
            live = await conn.run_sync(_snapshot, live_prefix)
            frozen = await conn.run_sync(_snapshot, frozen_prefix)
        yield live, frozen
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(metadata.drop_all)
            await conn.run_sync(_downgrade, frozen_prefix)


async def test_define_tables_and_schema_v1_are_identical(both) -> None:
    live, frozen = both
    assert len(live) == len(frozen) == TABLE_COUNT
    assert live.keys() == frozen.keys()
    for table in live:
        for part in ("columns", "primary_key", "uniques", "checks", "foreign_keys", "indexes"):
            assert live[table][part] == frozen[table][part], f"{table}.{part} differs"


async def test_snapshot_covers_extended_constraints(both) -> None:
    # Guards against a parity test that compares empty or partial snapshots.
    live, _ = both
    settlements = live["settlements"]
    assert any("amount_vnd = intent_amount_vnd" in text for _, text in settlements["checks"])
    assert {fk[0] for fk in settlements["foreign_keys"]} == {
        "settlements_transaction_fk",
        "settlements_intent_fk",
        "settlements_review_case_fk",
    }
    wheres = {ix[0]: ix[3] for ix in live["provider_transactions"]["indexes"]}
    assert wheres["transactions_webhook_tx_uq"] == "(webhook_tx_id IS NOT NULL)"
    profile_wheres = {ix[0]: ix[3] for ix in live["reference_profiles"]["indexes"]}
    assert profile_wheres["profiles_one_active_uq"] == "((status)::text = 'active'::text)"
    assert ("accounts_env_fingerprint_uq", ("environment", "account_fingerprint")) in live[
        "receiving_accounts"
    ]["uniques"]
    inbox_columns = {c["name"]: c for c in live["webhook_inbox"]["columns"]}
    assert inbox_columns["lease_generation"]["type"] == "BIGINT"
    assert inbox_columns["lease_generation"]["nullable"] is False
    assert inbox_columns["body_sha256"]["nullable"] is False
    assert inbox_columns["headers"]["nullable"] is True
    outbox_columns = {c["name"] for c in live["outbox_events"]["columns"]}
    assert {"event_type", "lease_owner", "lease_until", "lease_generation"} <= outbox_columns


async def test_every_timestamp_column_is_timezone_aware(both) -> None:
    # Absolute check, so drifting both schemas together still fails.
    for schema in both:
        timestamps = [
            (table, column["name"], column["type"])
            for table, parts in schema.items()
            for column in parts["columns"]
            if column["type"].startswith("TIMESTAMP")
        ]
        assert timestamps
        for table, name, type_ in timestamps:
            assert type_ == "TIMESTAMP WITH TIME ZONE", f"{table}.{name} is {type_}"


async def test_every_foreign_key_targets_an_exact_unique_key(both) -> None:
    live, _ = both
    for table, parts in live.items():
        for name, _, referred_table, referred_columns, _ in parts["foreign_keys"]:
            target = live[referred_table]
            keys = {frozenset(columns) for _, columns in target["uniques"]}
            keys.add(frozenset(target["primary_key"][1]))
            assert frozenset(referred_columns) in keys, f"{table}.{name} has no unique target"


async def test_downgrade_drops_every_table(engine: AsyncEngine) -> None:
    prefix = unique_prefix()
    async with engine.begin() as conn:
        await conn.run_sync(_upgrade, prefix)
        await conn.run_sync(_downgrade, prefix)
    async with engine.connect() as conn:
        remaining = await conn.run_sync(
            lambda sync: [t for t in sa.inspect(sync).get_table_names() if t.startswith(prefix)]
        )
    assert remaining == []
