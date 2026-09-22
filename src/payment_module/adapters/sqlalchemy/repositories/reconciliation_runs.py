"""Reconciliation runs of one connection."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa

from payment_module.adapters.sqlalchemy.repositories import SqlAlchemyRepository
from payment_module.domain.enums import ReconciliationRunStatus
from payment_module.ports.unit_of_work import ReadTarget, RunCheckpoint

ACCOUNT_REF_KEY = "account_ref"


class SqlAlchemyReconciliationRunRepository(SqlAlchemyRepository):
    async def start(
        self,
        *,
        tenant_id: str,
        connection_id: UUID,
        window_from: datetime,
        window_to: datetime,
        started_at: datetime,
        cursor: str | None = None,
        account_ref: str | None = None,
    ) -> UUID:
        """``account_ref`` names the provider-side account the run reads; it is kept in
        ``counts`` so each account has its own checkpoint."""
        run_id = uuid.uuid4()
        await self._session.execute(
            self._tables.reconciliation_runs.insert().values(
                id=run_id,
                tenant_id=tenant_id,
                connection_id=connection_id,
                window_from=window_from,
                window_to=window_to,
                cursor=cursor,
                status=ReconciliationRunStatus.RUNNING.value,
                counts={} if account_ref is None else {ACCOUNT_REF_KEY: account_ref},
                started_at=started_at,
            )
        )
        return run_id

    async def finish(
        self,
        run_id: UUID,
        *,
        status: ReconciliationRunStatus,
        counts: Mapping[str, int],
        cursor: str | None,
        finished_at: datetime,
        last_error: str | None = None,
    ) -> None:
        """Close the run; ``cursor`` is the next page to read, ``None`` when the window ended."""
        t = self._tables.reconciliation_runs
        started = await self._session.scalar(sa.select(t.c.counts).where(t.c.id == run_id))
        await self._session.execute(
            sa.update(t)
            .where(t.c.id == run_id)
            .values(
                status=ReconciliationRunStatus(status).value,
                counts={**(started or {}), **counts},
                cursor=cursor,
                finished_at=finished_at,
                last_error=last_error,
            )
        )

    async def last_checkpoint(self, connection_id: UUID, account_ref: str) -> RunCheckpoint | None:
        """The window and next cursor of the account's last completed run, if it left one."""
        t = self._tables.reconciliation_runs
        row = (
            await self._session.execute(
                sa.select(t.c.window_from, t.c.window_to, t.c.cursor)
                .where(
                    t.c.connection_id == connection_id,
                    t.c.status == ReconciliationRunStatus.COMPLETED.value,
                    t.c.counts[ACCOUNT_REF_KEY].as_string() == account_ref,
                )
                .order_by(t.c.finished_at.desc(), t.c.started_at.desc())
                .limit(1)
            )
        ).first()
        if row is None or row.cursor is None:
            return None
        return RunCheckpoint(row.window_from, row.window_to, row.cursor)

    async def read_targets(self, connection_id: UUID) -> Sequence[ReadTarget]:
        """Accounts bound to the connection with their provider-side reference, by id."""
        accounts = self._tables.receiving_accounts
        bindings = self._tables.connection_account_bindings
        rows = await self._session.execute(
            sa.select(accounts.c.id, accounts.c.provider_account_ref)
            .join(bindings, bindings.c.receiving_account_id == accounts.c.id)
            .where(bindings.c.connection_id == connection_id)
            .order_by(accounts.c.id)
        )
        return [ReadTarget(row.id, row.provider_account_ref) for row in rows]
