"""Outbox: events committed with domain changes, delivered at least once."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from uuid import UUID

import sqlalchemy as sa

from payment_module.adapters.sqlalchemy.repositories import SqlAlchemyRepository
from payment_module.domain.enums import OutboxStatus
from payment_module.ports.publisher import OutboxEventView
from payment_module.ports.unit_of_work import ClaimedOutbox


class SqlAlchemyOutboxRepository(SqlAlchemyRepository):
    async def add(
        self,
        event: OutboxEventView,
        aggregate_type: str,
        aggregate_id: UUID,
        available_at: datetime,
    ) -> None:
        tenant_id = event.trusted_scope.get("tenant_id")
        if not isinstance(tenant_id, str):
            raise ValueError("outbox events need trusted_scope['tenant_id']")
        await self._session.execute(
            self._tables.outbox_events.insert().values(
                event_id=event.event_id,
                tenant_id=tenant_id,
                event_type=event.event_type,
                schema_version=event.schema_version,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                payload=dict(event.payload),
                trusted_scope=dict(event.trusted_scope),
                status=OutboxStatus.PENDING.value,
                occurred_at=event.occurred_at,
                available_at=available_at,
            )
        )

    async def claim_batch(
        self, limit: int, lease_seconds: int, owner: str, now: datetime
    ) -> Sequence[ClaimedOutbox]:
        """Claim due ``pending`` events whose lease is absent or expired (recovery path).

        Uses ``FOR UPDATE SKIP LOCKED``; each claim bumps ``lease_generation`` and attempts.
        """
        t = self._tables.outbox_events
        picked = (
            sa.select(t.c.event_id)
            .where(
                t.c.status == OutboxStatus.PENDING.value,
                t.c.available_at <= now,
                sa.or_(t.c.next_attempt_at.is_(None), t.c.next_attempt_at <= now),
                sa.or_(t.c.lease_until.is_(None), t.c.lease_until < now),
            )
            .order_by(t.c.available_at, t.c.event_id)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .cte("picked")
        )
        rows = await self._session.execute(
            sa.update(t)
            .where(t.c.event_id.in_(sa.select(picked.c.event_id)))
            .values(
                lease_owner=owner,
                lease_until=now + timedelta(seconds=lease_seconds),
                lease_generation=t.c.lease_generation + 1,
                attempts=t.c.attempts + 1,
            )
            .returning(t)
        )
        claimed = [
            (
                row.available_at,
                ClaimedOutbox(
                    event=OutboxEventView(
                        event_id=row.event_id,
                        event_type=row.event_type,
                        schema_version=row.schema_version,
                        trusted_scope=row.trusted_scope,
                        payload=row.payload,
                        occurred_at=row.occurred_at,
                    ),
                    attempts=row.attempts,
                    lease_generation=row.lease_generation,
                ),
            )
            for row in rows
        ]
        claimed.sort(key=lambda item: (item[0], item[1].event.event_id))
        return [item for _, item in claimed]

    async def finalize(
        self,
        event_id: UUID,
        lease_generation: int,
        status: OutboxStatus,
        *,
        now: datetime,
        next_attempt_at: datetime | None = None,
        last_error: str | None = None,
    ) -> bool:
        """Record the publish result only if this lease is still current.

        ``published`` stamps ``published_at``; ``pending`` schedules a retry at
        ``next_attempt_at``; ``failed`` stops delivery. The lease is released either way.
        """
        t = self._tables.outbox_events
        status = OutboxStatus(status)
        result = await self._session.execute(
            sa.update(t)
            .where(
                t.c.event_id == event_id,
                t.c.lease_generation == lease_generation,
                t.c.status == OutboxStatus.PENDING.value,
            )
            .values(
                status=status.value,
                lease_owner=None,
                lease_until=None,
                next_attempt_at=next_attempt_at,
                last_error=last_error,
                published_at=now if status == OutboxStatus.PUBLISHED else None,
            )
        )
        return result.rowcount == 1

    async def requeue(self, event_id: UUID) -> bool:
        """``failed -> pending``, with attempts and retry state reset."""
        t = self._tables.outbox_events
        result = await self._session.execute(
            sa.update(t)
            .where(t.c.event_id == event_id, t.c.status == OutboxStatus.FAILED.value)
            .values(
                status=OutboxStatus.PENDING.value,
                attempts=0,
                next_attempt_at=None,
                lease_owner=None,
                lease_until=None,
            )
        )
        return result.rowcount == 1
