"""Settlements: the only link between money and an intent, exact amount only."""

from __future__ import annotations

import uuid
from datetime import datetime
from uuid import UUID

from payment_module.adapters.sqlalchemy.repositories import SqlAlchemyRepository
from payment_module.domain.enums import Environment, SettlementOrigin
from payment_module.domain.money import AmountVnd


class SqlAlchemySettlementRepository(SqlAlchemyRepository):
    async def add(
        self,
        *,
        tenant_id: str,
        environment: Environment,
        transaction_id: UUID,
        intent_id: UUID,
        receiving_account_id: UUID,
        amount: AmountVnd,
        intent_amount: AmountVnd,
        origin: SettlementOrigin,
        settled_at: datetime,
        review_case_id: UUID | None = None,
        resolved_by: str | None = None,
    ) -> UUID:
        """Both amounts are stored; the database rejects any difference and any scope drift."""
        settlement_id = uuid.uuid4()
        await self._session.execute(
            self._tables.settlements.insert().values(
                id=settlement_id,
                tenant_id=tenant_id,
                environment=Environment(environment).value,
                transaction_id=transaction_id,
                intent_id=intent_id,
                receiving_account_id=receiving_account_id,
                amount_vnd=amount.value,
                intent_amount_vnd=intent_amount.value,
                origin=SettlementOrigin(origin).value,
                review_case_id=review_case_id,
                resolved_by=resolved_by,
                settled_at=settled_at,
            )
        )
        return settlement_id
