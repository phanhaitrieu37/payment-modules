"""Webhook inbox: durable intake, claim with lease fencing, compare-and-set finalize."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from payment_module.adapters.sqlalchemy.repositories import SqlAlchemyRepository
from payment_module.domain.enums import EventKeyKind, InboxStatus
from payment_module.ports.unit_of_work import ClaimedInbox

_CLAIMABLE = (InboxStatus.RECEIVED.value, InboxStatus.RETRY_WAIT.value)
_REQUEUEABLE = (InboxStatus.FAILED.value, InboxStatus.QUARANTINED.value)
_PURGEABLE = (
    InboxStatus.PROCESSED.value,
    InboxStatus.FAILED.value,
    InboxStatus.QUARANTINED.value,
)


class SqlAlchemyInboxRepository(SqlAlchemyRepository):
    async def add(
        self,
        *,
        tenant_id: str,
        connection_id: UUID,
        event_key: str,
        event_key_kind: EventKeyKind,
        body_sha256: str,
        raw_body: bytes,
        headers: Mapping[str, str],
        received_at: datetime,
        status: InboxStatus = InboxStatus.RECEIVED,
        last_error_code: str | None = None,
        purge_after: datetime | None = None,
    ) -> UUID | None:
        """Insert a delivery; ``None`` when ``(connection_id, event_key)`` already exists."""
        t = self._tables.webhook_inbox
        return await self._session.scalar(
            pg_insert(t)
            .values(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                connection_id=connection_id,
                event_key=event_key,
                event_key_kind=EventKeyKind(event_key_kind).value,
                body_sha256=body_sha256,
                raw_body=raw_body,
                headers=dict(headers),
                received_at=received_at,
                status=InboxStatus(status).value,
                last_error_code=last_error_code,
                purge_after=purge_after,
            )
            .on_conflict_do_nothing(index_elements=[t.c.connection_id, t.c.event_key])
            .returning(t.c.id)
        )

    async def claim_batch(
        self, limit: int, lease_seconds: int, owner: str, now: datetime
    ) -> Sequence[ClaimedInbox]:
        """Claim due deliveries and expired leases with ``FOR UPDATE SKIP LOCKED``.

        Each claim sets ``processing``, a new lease and ``lease_generation + 1``. Rows whose
        raw body was purged (or is missing) are never claimed: :class:`ClaimedInbox` always
        carries the raw body.
        """
        t = self._tables.webhook_inbox
        due = self._due(now)
        picked = (
            sa.select(t.c.id)
            .where(due, t.c.raw_purged_at.is_(None), t.c.raw_body.is_not(None))
            .order_by(t.c.received_at, t.c.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .cte("picked")
        )
        rows = await self._session.execute(
            sa.update(t)
            .where(t.c.id.in_(sa.select(picked.c.id)))
            .values(
                status=InboxStatus.PROCESSING.value,
                lease_owner=owner,
                lease_until=now + timedelta(seconds=lease_seconds),
                lease_generation=t.c.lease_generation + 1,
                attempts=t.c.attempts + 1,
            )
            .returning(
                t.c.id,
                t.c.tenant_id,
                t.c.connection_id,
                t.c.event_key,
                t.c.raw_body,
                t.c.received_at,
                t.c.attempts,
                t.c.lease_generation,
                t.c.headers,
            )
        )
        claimed = [_claimed(row) for row in rows]
        return sorted(claimed, key=lambda item: (item.received_at, item.id))

    async def claim_one(
        self, inbox_id: UUID, lease_seconds: int, owner: str, now: datetime
    ) -> ClaimedInbox | None:
        """Claim one due delivery; ``None`` when it is not due or another claim holds it.

        ``SKIP LOCKED`` makes a row locked by a worker's claim or processing transaction
        look absent, so an inline caller never waits for the worker.
        """
        t = self._tables.webhook_inbox
        picked = (
            sa.select(t.c.id)
            .where(
                t.c.id == inbox_id,
                self._due(now),
                t.c.raw_purged_at.is_(None),
                t.c.raw_body.is_not(None),
            )
            .with_for_update(skip_locked=True)
            .cte("picked")
        )
        row = (
            await self._session.execute(
                sa.update(t)
                .where(t.c.id.in_(sa.select(picked.c.id)))
                .values(
                    status=InboxStatus.PROCESSING.value,
                    lease_owner=owner,
                    lease_until=now + timedelta(seconds=lease_seconds),
                    lease_generation=t.c.lease_generation + 1,
                    attempts=t.c.attempts + 1,
                )
                .returning(
                    t.c.id,
                    t.c.tenant_id,
                    t.c.connection_id,
                    t.c.event_key,
                    t.c.raw_body,
                    t.c.received_at,
                    t.c.attempts,
                    t.c.lease_generation,
                    t.c.headers,
                )
            )
        ).first()
        return None if row is None else _claimed(row)

    def _due(self, now: datetime) -> sa.ColumnElement[bool]:
        t = self._tables.webhook_inbox
        return sa.or_(
            sa.and_(
                t.c.status.in_(_CLAIMABLE),
                sa.or_(t.c.next_attempt_at.is_(None), t.c.next_attempt_at <= now),
            ),
            sa.and_(t.c.status == InboxStatus.PROCESSING.value, t.c.lease_until < now),
        )

    async def lock_claim(self, inbox_id: UUID, lease_generation: int) -> bool:
        """Lock the row while it is still ``processing`` under ``lease_generation``.

        Holding this lock for the whole processing transaction keeps a reclaim from picking
        the row until this owner commits or rolls back.
        """
        t = self._tables.webhook_inbox
        found = await self._session.scalar(
            sa.select(t.c.id)
            .where(
                t.c.id == inbox_id,
                t.c.lease_generation == lease_generation,
                t.c.status == InboxStatus.PROCESSING.value,
            )
            .with_for_update()
        )
        return found is not None

    async def find_id(self, connection_id: UUID, event_key: str) -> UUID | None:
        t = self._tables.webhook_inbox
        return await self._session.scalar(
            sa.select(t.c.id).where(t.c.connection_id == connection_id, t.c.event_key == event_key)
        )

    async def get_status(self, inbox_id: UUID) -> InboxStatus | None:
        t = self._tables.webhook_inbox
        status = await self._session.scalar(sa.select(t.c.status).where(t.c.id == inbox_id))
        return None if status is None else InboxStatus(status)

    async def requeue(self, inbox_id: UUID) -> bool:
        """``failed | quarantined -> received``, with attempts and retry state reset.

        A row whose raw body was purged is refused: no worker could ever claim it again.
        """
        t = self._tables.webhook_inbox
        result = await self._session.execute(
            sa.update(t)
            .where(t.c.id == inbox_id, t.c.status.in_(_REQUEUEABLE), t.c.raw_purged_at.is_(None))
            .values(
                status=InboxStatus.RECEIVED.value,
                attempts=0,
                next_attempt_at=None,
                last_error_code=None,
                lease_owner=None,
                lease_until=None,
            )
        )
        return result.rowcount == 1

    async def purge_expired(self, now: datetime, limit: int) -> int:
        """Drop the raw body and headers of finished rows past ``purge_after``."""
        t = self._tables.webhook_inbox
        return await self._update_batch(
            t,
            sa.and_(
                t.c.purge_after < now,
                t.c.raw_purged_at.is_(None),
                t.c.status.in_(_PURGEABLE),
            ),
            {"raw_body": None, "headers": sa.null(), "raw_purged_at": now},
            limit,
        )

    async def finalize(
        self,
        inbox_id: UUID,
        lease_generation: int,
        status: InboxStatus,
        *,
        next_attempt_at: datetime | None = None,
        last_error_code: str | None = None,
    ) -> bool:
        """Leave ``processing`` only if this lease is still current, and release the lease."""
        t = self._tables.webhook_inbox
        result = await self._session.execute(
            sa.update(t)
            .where(
                t.c.id == inbox_id,
                t.c.lease_generation == lease_generation,
                t.c.status == InboxStatus.PROCESSING.value,
            )
            .values(
                status=InboxStatus(status).value,
                lease_owner=None,
                lease_until=None,
                next_attempt_at=next_attempt_at,
                last_error_code=last_error_code,
            )
        )
        return result.rowcount == 1


def _claimed(row: sa.Row) -> ClaimedInbox:
    return ClaimedInbox(
        id=row.id,
        tenant_id=row.tenant_id,
        connection_id=row.connection_id,
        event_key=row.event_key,
        raw_body=row.raw_body,
        received_at=row.received_at,
        attempts=row.attempts,
        lease_generation=row.lease_generation,
        headers=dict(row.headers or {}),
    )
