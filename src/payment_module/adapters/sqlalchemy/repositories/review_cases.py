"""Review cases: money that needs a human decision; one open case per fact."""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping, Sequence
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa

from payment_module.adapters.sqlalchemy.repositories import SqlAlchemyRepository
from payment_module.domain.enums import (
    Environment,
    ReviewCaseStatus,
    ReviewReason,
    ReviewResolution,
)
from payment_module.domain.events import JsonValue
from payment_module.ports.unit_of_work import ReviewCaseView


class SqlAlchemyReviewCaseRepository(SqlAlchemyRepository):
    async def open(
        self,
        *,
        tenant_id: str,
        environment: Environment,
        transaction_id: UUID,
        reason: ReviewReason,
        details: Mapping[str, str],
        opened_at: datetime,
        candidate_intent_id: UUID | None = None,
    ) -> UUID:
        case_id = uuid.uuid4()
        await self._session.execute(
            self._tables.review_cases.insert().values(
                id=case_id,
                tenant_id=tenant_id,
                environment=Environment(environment).value,
                transaction_id=transaction_id,
                candidate_intent_id=candidate_intent_id,
                reason=ReviewReason(reason).value,
                details=dict(details),
                status=ReviewCaseStatus.OPEN.value,
                opened_at=opened_at,
            )
        )
        return case_id

    async def find_open(self, transaction_id: UUID) -> UUID | None:
        """Id of the fact's open case, locked ``FOR UPDATE``."""
        t = self._tables.review_cases
        return await self._session.scalar(
            sa.select(t.c.id)
            .where(t.c.transaction_id == transaction_id, t.c.status == ReviewCaseStatus.OPEN.value)
            .with_for_update()
        )

    async def update_open(
        self,
        case_id: UUID,
        reason: ReviewReason,
        details: Mapping[str, str],
        candidate_intent_id: UUID | None,
    ) -> bool:
        """Replace reason, details and candidate of a case that is still open."""
        t = self._tables.review_cases
        result = await self._session.execute(
            sa.update(t)
            .where(t.c.id == case_id, t.c.status == ReviewCaseStatus.OPEN.value)
            .values(
                reason=ReviewReason(reason).value,
                details=dict(details),
                candidate_intent_id=candidate_intent_id,
            )
        )
        return result.rowcount == 1

    async def get(self, tenant_id: str, case_id: UUID) -> ReviewCaseView | None:
        t = self._tables.review_cases
        return await self._one(sa.select(t).where(t.c.tenant_id == tenant_id, t.c.id == case_id))

    async def get_for_update(self, case_id: UUID) -> ReviewCaseView | None:
        t = self._tables.review_cases
        return await self._one(sa.select(t).where(t.c.id == case_id).with_for_update())

    async def resolve(
        self,
        case_id: UUID,
        *,
        resolution: ReviewResolution,
        resolved_by: str,
        resolution_ref: str | None,
        resolution_note: str | None,
        resolved_at: datetime,
        details: Mapping[str, JsonValue] | None = None,
    ) -> bool:
        t = self._tables.review_cases
        values: dict[str, object] = {
            "status": ReviewCaseStatus.RESOLVED.value,
            "resolution": ReviewResolution(resolution).value,
            "resolution_ref": resolution_ref,
            "resolved_by": resolved_by,
            "resolution_note": resolution_note,
            "resolved_at": resolved_at,
        }
        if details is not None:
            values["details"] = dict(details)
        result = await self._session.execute(
            sa.update(t)
            .where(t.c.id == case_id, t.c.status == ReviewCaseStatus.OPEN.value)
            .values(**values)
        )
        return result.rowcount == 1

    async def list_open(
        self,
        tenant_id: str,
        reasons: Collection[ReviewReason],
        *,
        connection_id: UUID | None = None,
        opened_after: datetime | None = None,
    ) -> Sequence[ReviewCaseView]:
        t = self._tables.review_cases
        query = sa.select(t).where(
            t.c.tenant_id == tenant_id,
            t.c.status == ReviewCaseStatus.OPEN.value,
            t.c.reason.in_([ReviewReason(reason).value for reason in reasons]),
        )
        if opened_after is not None:
            query = query.where(t.c.opened_at >= opened_after)
        if connection_id is not None:
            o = self._tables.provider_observations
            query = query.where(
                sa.exists().where(
                    o.c.transaction_id == t.c.transaction_id, o.c.connection_id == connection_id
                )
            )
        rows = await self._session.execute(query.order_by(t.c.opened_at, t.c.id))
        return [_view(row) for row in rows]

    async def _one(self, query: sa.Select) -> ReviewCaseView | None:
        row = (await self._session.execute(query)).first()
        return None if row is None else _view(row)


def _view(row: sa.Row) -> ReviewCaseView:
    return ReviewCaseView(
        id=row.id,
        tenant_id=row.tenant_id,
        environment=row.environment,
        transaction_id=row.transaction_id,
        candidate_intent_id=row.candidate_intent_id,
        reason=row.reason,
        details=dict(row.details or {}),
        status=row.status,
        resolution=row.resolution,
        resolution_ref=row.resolution_ref,
        resolved_by=row.resolved_by,
        resolution_note=row.resolution_note,
        opened_at=row.opened_at,
        resolved_at=row.resolved_at,
    )
