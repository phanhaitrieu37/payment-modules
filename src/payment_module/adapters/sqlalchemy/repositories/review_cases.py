"""Review cases: money that needs a human decision; one open case per fact."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa

from payment_module.adapters.sqlalchemy.repositories import SqlAlchemyRepository
from payment_module.domain.enums import Environment, ReviewCaseStatus, ReviewReason


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
