"""Inbox and outbox claims: SKIP LOCKED batches, lease expiry and generation fencing."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from payment_module.adapters.sqlalchemy.repositories.inbox import SqlAlchemyInboxRepository
from payment_module.adapters.sqlalchemy.repositories.outbox import SqlAlchemyOutboxRepository
from payment_module.adapters.sqlalchemy.tables import PaymentTables
from payment_module.adapters.sqlalchemy.uow import SqlAlchemyUnitOfWork
from payment_module.domain.enums import Environment, EventKeyKind, InboxStatus, OutboxStatus
from payment_module.ports.publisher import OutboxEventView

pytestmark = [pytest.mark.postgres, pytest.mark.timeout(600)]

LEASE = 30
NOW = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)


@dataclass
class Env:
    sessions: async_sessionmaker[AsyncSession]
    tables: PaymentTables
    tenant_id: str
    connection_id: uuid.UUID

    def inbox(self, session: AsyncSession) -> SqlAlchemyInboxRepository:
        return SqlAlchemyInboxRepository(session, self.tables)

    def outbox(self, session: AsyncSession) -> SqlAlchemyOutboxRepository:
        return SqlAlchemyOutboxRepository(session, self.tables)


@pytest.fixture
async def env(engine: AsyncEngine, tables: PaymentTables) -> AsyncIterator[Env]:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with SqlAlchemyUnitOfWork(sessions, tables) as uow:
        merchant_id = await uow.merchants.add("tenant-a", "shop-1")
        connection_id = await uow.connections.add(
            tenant_id="tenant-a",
            merchant_id=merchant_id,
            provider="sepay",
            environment=Environment.TEST,
            locator=uuid.uuid4().hex,
            secret_ref="env:SEPAY_WEBHOOK_SECRET",
        )
        await uow.commit()
    yield Env(sessions, tables, "tenant-a", connection_id)


async def _deliveries(env: Env, count: int) -> list[uuid.UUID]:
    ids = []
    async with env.sessions.begin() as session:
        for number in range(count):
            inbox_id = await env.inbox(session).add(
                tenant_id=env.tenant_id,
                connection_id=env.connection_id,
                event_key=f"webhook:{number}",
                event_key_kind=EventKeyKind.PROVIDER_ID,
                body_sha256="0" * 64,
                raw_body=b'{"id": %d}' % number,
                headers={"content-type": "application/json"},
                received_at=NOW + timedelta(seconds=number),
            )
            assert inbox_id is not None
            ids.append(inbox_id)
    return ids


async def _inbox_row(env: Env, inbox_id: uuid.UUID) -> sa.Row:
    t = env.tables.webhook_inbox
    async with env.sessions() as session:
        return (await session.execute(sa.select(t).where(t.c.id == inbox_id))).one()


async def test_duplicate_delivery_is_not_inserted_twice(env: Env) -> None:
    await _deliveries(env, 1)
    async with env.sessions.begin() as session:
        again = await env.inbox(session).add(
            tenant_id=env.tenant_id,
            connection_id=env.connection_id,
            event_key="webhook:0",
            event_key_kind=EventKeyKind.PROVIDER_ID,
            body_sha256="1" * 64,
            raw_body=b"{}",
            headers={},
            received_at=NOW,
        )
    assert again is None


async def test_parallel_inbox_claims_never_overlap(env: Env) -> None:
    ids = await _deliveries(env, 6)
    # Two sessions on two pooled connections: the first keeps its row locks open while the
    # second claims, so SKIP LOCKED must hand the second one the remaining rows only.
    async with env.sessions() as first, env.sessions() as second:
        claimed_first = await env.inbox(first).claim_batch(4, LEASE, "worker-1", NOW)
        claimed_second = await env.inbox(second).claim_batch(10, LEASE, "worker-2", NOW)
        await first.commit()
        await second.commit()
    first_ids = {item.id for item in claimed_first}
    second_ids = {item.id for item in claimed_second}
    assert len(first_ids) == 4
    assert first_ids.isdisjoint(second_ids)
    assert first_ids | second_ids == set(ids)
    assert all(item.lease_generation == 1 and item.attempts == 1 for item in claimed_first)
    assert claimed_first[0].raw_body == b'{"id": 0}'


async def test_active_lease_is_not_reclaimed(env: Env) -> None:
    await _deliveries(env, 1)
    async with env.sessions.begin() as session:
        assert len(await env.inbox(session).claim_batch(10, LEASE, "worker-1", NOW)) == 1
    async with env.sessions.begin() as session:
        later = NOW + timedelta(seconds=LEASE - 1)
        assert await env.inbox(session).claim_batch(10, LEASE, "worker-2", later) == []


async def test_expired_inbox_lease_is_reclaimed_with_new_generation(env: Env) -> None:
    [inbox_id] = await _deliveries(env, 1)
    async with env.sessions.begin() as session:
        await env.inbox(session).claim_batch(10, LEASE, "worker-1", NOW)
    async with env.sessions.begin() as session:
        expired = NOW + timedelta(seconds=LEASE + 1)
        [reclaimed] = await env.inbox(session).claim_batch(10, LEASE, "worker-2", expired)
    assert reclaimed.id == inbox_id
    assert reclaimed.lease_generation == 2
    assert reclaimed.attempts == 2
    assert (await _inbox_row(env, inbox_id)).lease_owner == "worker-2"


async def test_stale_inbox_owner_cannot_finalize_after_reclaim(env: Env) -> None:
    [inbox_id] = await _deliveries(env, 1)
    async with env.sessions.begin() as session:
        [old] = await env.inbox(session).claim_batch(10, LEASE, "worker-1", NOW)
    async with env.sessions.begin() as session:
        expired = NOW + timedelta(seconds=LEASE + 1)
        [new] = await env.inbox(session).claim_batch(10, LEASE, "worker-2", expired)
    async with env.sessions.begin() as session:
        assert not await env.inbox(session).finalize(
            inbox_id, old.lease_generation, InboxStatus.PROCESSED
        )
    row = await _inbox_row(env, inbox_id)
    assert (row.status, row.lease_owner, row.lease_generation) == (
        InboxStatus.PROCESSING.value,
        "worker-2",
        2,
    )
    async with env.sessions.begin() as session:
        assert await env.inbox(session).finalize(
            inbox_id, new.lease_generation, InboxStatus.PROCESSED
        )
    row = await _inbox_row(env, inbox_id)
    assert (row.status, row.lease_owner, row.lease_until) == (
        InboxStatus.PROCESSED.value,
        None,
        None,
    )


async def test_retry_wait_is_claimed_only_when_due(env: Env) -> None:
    [inbox_id] = await _deliveries(env, 1)
    async with env.sessions.begin() as session:
        [claimed] = await env.inbox(session).claim_batch(10, LEASE, "worker-1", NOW)
        retry_at = NOW + timedelta(minutes=5)
        assert await env.inbox(session).finalize(
            inbox_id,
            claimed.lease_generation,
            InboxStatus.RETRY_WAIT,
            next_attempt_at=retry_at,
            last_error_code="handler_error",
        )
    async with env.sessions.begin() as session:
        early = retry_at - timedelta(seconds=1)
        assert await env.inbox(session).claim_batch(10, LEASE, "worker-1", early) == []
        [again] = await env.inbox(session).claim_batch(10, LEASE, "worker-1", retry_at)
    assert again.lease_generation == 2


@pytest.mark.parametrize("status", [InboxStatus.PROCESSED, InboxStatus.FAILED])
async def test_finished_deliveries_are_not_claimed(env: Env, status: InboxStatus) -> None:
    [inbox_id] = await _deliveries(env, 1)
    async with env.sessions.begin() as session:
        [claimed] = await env.inbox(session).claim_batch(10, LEASE, "worker-1", NOW)
        await env.inbox(session).finalize(inbox_id, claimed.lease_generation, status)
    async with env.sessions.begin() as session:
        later = NOW + timedelta(days=1)
        assert await env.inbox(session).claim_batch(10, LEASE, "worker-1", later) == []


async def test_purged_delivery_is_never_claimed(env: Env) -> None:
    [inbox_id] = await _deliveries(env, 1)
    t = env.tables.webhook_inbox
    async with env.sessions.begin() as session:
        await session.execute(
            sa.update(t).where(t.c.id == inbox_id).values(raw_body=None, raw_purged_at=NOW)
        )
    async with env.sessions.begin() as session:
        assert await env.inbox(session).claim_batch(10, LEASE, "worker-1", NOW) == []


# Outbox.


async def _events(env: Env, count: int) -> list[uuid.UUID]:
    ids = []
    async with env.sessions.begin() as session:
        for number in range(count):
            event_id = uuid.uuid4()
            await env.outbox(session).add(
                OutboxEventView(
                    event_id=event_id,
                    event_type="PaymentSettled",
                    schema_version=1,
                    trusted_scope={"tenant_id": env.tenant_id, "environment": "test"},
                    payload={"n": number},
                    occurred_at=NOW,
                ),
                aggregate_type="payment_intent",
                aggregate_id=uuid.uuid4(),
                available_at=NOW + timedelta(seconds=number),
            )
            ids.append(event_id)
    return ids


async def _outbox_row(env: Env, event_id: uuid.UUID) -> sa.Row:
    t = env.tables.outbox_events
    async with env.sessions() as session:
        return (await session.execute(sa.select(t).where(t.c.event_id == event_id))).one()


async def test_parallel_outbox_claims_never_overlap(env: Env) -> None:
    ids = await _events(env, 5)
    later = NOW + timedelta(minutes=1)
    async with env.sessions() as first, env.sessions() as second:
        claimed_first = await env.outbox(first).claim_batch(2, LEASE, "pub-1", later)
        claimed_second = await env.outbox(second).claim_batch(10, LEASE, "pub-2", later)
        await first.commit()
        await second.commit()
    first_ids = {item.event.event_id for item in claimed_first}
    second_ids = {item.event.event_id for item in claimed_second}
    assert len(first_ids) == 2
    assert first_ids.isdisjoint(second_ids)
    assert first_ids | second_ids == set(ids)
    assert claimed_first[0].event.payload == {"n": 0}
    assert claimed_first[0].event.trusted_scope["tenant_id"] == env.tenant_id


async def test_outbox_event_is_not_claimed_before_available_at(env: Env) -> None:
    await _events(env, 1)
    async with env.sessions.begin() as session:
        early = NOW - timedelta(seconds=1)
        assert await env.outbox(session).claim_batch(10, LEASE, "pub-1", early) == []


async def test_expired_outbox_lease_is_recovered_and_old_owner_fenced(env: Env) -> None:
    [event_id] = await _events(env, 1)
    async with env.sessions.begin() as session:
        [old] = await env.outbox(session).claim_batch(10, LEASE, "pub-1", NOW)
    async with env.sessions.begin() as session:
        during = NOW + timedelta(seconds=LEASE - 1)
        assert await env.outbox(session).claim_batch(10, LEASE, "pub-2", during) == []
        expired = NOW + timedelta(seconds=LEASE + 1)
        [new] = await env.outbox(session).claim_batch(10, LEASE, "pub-2", expired)
    assert (new.lease_generation, new.attempts) == (2, 2)
    async with env.sessions.begin() as session:
        assert not await env.outbox(session).finalize(
            event_id, old.lease_generation, OutboxStatus.PUBLISHED, now=NOW
        )
    assert (await _outbox_row(env, event_id)).status == OutboxStatus.PENDING.value
    async with env.sessions.begin() as session:
        assert await env.outbox(session).finalize(
            event_id, new.lease_generation, OutboxStatus.PUBLISHED, now=NOW
        )
    row = await _outbox_row(env, event_id)
    assert (row.status, row.published_at, row.lease_owner) == (
        OutboxStatus.PUBLISHED.value,
        NOW,
        None,
    )
    async with env.sessions.begin() as session:
        much_later = NOW + timedelta(days=1)
        assert await env.outbox(session).claim_batch(10, LEASE, "pub-3", much_later) == []


async def test_outbox_retry_is_claimed_again_when_due(env: Env) -> None:
    [event_id] = await _events(env, 1)
    retry_at = NOW + timedelta(minutes=2)
    async with env.sessions.begin() as session:
        [claimed] = await env.outbox(session).claim_batch(10, LEASE, "pub-1", NOW)
        assert await env.outbox(session).finalize(
            event_id,
            claimed.lease_generation,
            OutboxStatus.PENDING,
            now=NOW,
            next_attempt_at=retry_at,
            last_error="consumer unavailable",
        )
    async with env.sessions.begin() as session:
        early = retry_at - timedelta(seconds=1)
        assert await env.outbox(session).claim_batch(10, LEASE, "pub-1", early) == []
        [again] = await env.outbox(session).claim_batch(10, LEASE, "pub-1", retry_at)
    assert again.event.event_id == event_id
    assert again.lease_generation == 2
