"""One repository per aggregate; every repository shares the unit of work's session.

Repositories never log or ``repr`` full account numbers or raw webhook bodies.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from payment_module.adapters.sqlalchemy.tables import PaymentTables
from payment_module.domain.enums import MatchState, ReviewCaseStatus

# A fact in one of these states is never matched again; only linking may still read its memo.
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

    def _free_text_is_spent(
        self, transaction_id: sa.ColumnElement[Any], linkable_after: datetime
    ) -> sa.ColumnElement[bool]:
        """Nothing will read the fact's free text again: it is decided and cannot be linked.

        Decided: the match state is final and no review case is open, since rematch and
        review resolution read reference tokens from the memo. Cannot be linked: a sighting
        from the other source is linked only after its reference tokens are compared with
        the first observation's memo, so the fact must already carry both source ids, or it
        and every observation of it must predate ``linkable_after``, the oldest fact a read
        can still link.
        """
        # Aliased so the subqueries never correlate with the table being purged.
        tx = self._tables.provider_transactions.alias("spent_tx")
        obs = self._tables.provider_observations.alias("spent_obs")
        cases = self._tables.review_cases
        unlinkable = sa.or_(
            sa.and_(tx.c.webhook_tx_id.is_not(None), tx.c.api_tx_id.is_not(None)),
            sa.and_(
                tx.c.created_at < linkable_after,
                ~sa.exists().where(
                    obs.c.transaction_id == transaction_id,
                    obs.c.observed_at >= linkable_after,
                ),
            ),
        )
        return sa.and_(
            sa.exists().where(
                tx.c.id == transaction_id,
                tx.c.match_state.in_(FINAL_MATCH_STATES),
                unlinkable,
            ),
            ~sa.exists().where(
                cases.c.transaction_id == transaction_id,
                cases.c.status == ReviewCaseStatus.OPEN.value,
            ),
        )
