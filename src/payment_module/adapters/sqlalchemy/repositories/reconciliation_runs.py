"""Reconciliation runs of one connection."""

from __future__ import annotations

import uuid
from datetime import datetime
from uuid import UUID

from payment_module.adapters.sqlalchemy.repositories import SqlAlchemyRepository
from payment_module.domain.enums import ReconciliationRunStatus


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
    ) -> UUID:
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
                counts={},
                started_at=started_at,
            )
        )
        return run_id
