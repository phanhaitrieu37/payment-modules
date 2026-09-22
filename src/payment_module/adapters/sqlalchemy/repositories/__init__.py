"""One repository per aggregate; every repository shares the unit of work's session.

Repositories never log or ``repr`` full account numbers or raw webhook bodies.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from payment_module.adapters.sqlalchemy.tables import PaymentTables
from payment_module.domain.enums import MatchState, ReviewCaseStatus

# A fact in one of these states is never matched again, so its free text is no longer read.
FINAL_MATCH_STATES = (
    MatchState.SETTLED.value,
    MatchState.NOT_APPLICABLE.value,
    MatchState.CLOSED_EXTERNAL.value,
    MatchState.DUPLICATE_OF.value,
)


class SqlAlchemyRepository:
    def __init__(self, session: AsyncSession, tables: PaymentTables) -> None:
        self._session = session
        self._tables = tables

    async def _update_batch(
        self, table: sa.Table, where: sa.ColumnElement[bool], values: dict[str, Any], limit: int
    ) -> int:
        """Update at most ``limit`` rows matching ``where``, skipping rows locked elsewhere."""
        picked = (
            sa.select(table.c.id)
            .where(where)
            .order_by(table.c.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .cte("picked")
        )
        result = await self._session.execute(
            sa.update(table).where(table.c.id.in_(sa.select(picked.c.id))).values(**values)
        )
        return result.rowcount

    def _fact_is_decided(self, transaction_id: sa.ColumnElement[Any]) -> sa.ColumnElement[bool]:
        """The fact exists, its match state is final and it has no open review case.

        Rematch, review resolution and cross-source linking read reference tokens from the
        memo, so free text may only be purged once nothing will decide the fact again.
        """
        tx = self._tables.provider_transactions
        cases = self._tables.review_cases
        return sa.and_(
            sa.exists().where(tx.c.id == transaction_id, tx.c.match_state.in_(FINAL_MATCH_STATES)),
            ~sa.exists().where(
                cases.c.transaction_id == transaction_id,
                cases.c.status == ReviewCaseStatus.OPEN.value,
            ),
        )
