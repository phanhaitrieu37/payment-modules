"""Canonical provider transactions (one row per movement of money)."""

from __future__ import annotations

import uuid
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from payment_module.adapters.sqlalchemy.repositories import SqlAlchemyRepository
from payment_module.domain.enums import Environment, IdentityKind, MatchState
from payment_module.domain.money import AmountVnd
from payment_module.domain.transaction import TransactionView
from payment_module.ports.unit_of_work import NewProviderTransaction


class SqlAlchemyTransactionRepository(SqlAlchemyRepository):
    async def insert_or_get_by_dedup_key(
        self, new: NewProviderTransaction
    ) -> tuple[TransactionView, bool]:
        """``INSERT ... ON CONFLICT (tenant_id, environment, dedup_key) DO NOTHING``.

        On conflict the existing fact is read by the same key and returned with ``False``.
        Only the dedup key is a tolerated conflict; any other unique violation raises.
        """
        t = self._tables.provider_transactions
        by_webhook = new.identity_kind == IdentityKind.WEBHOOK_ID
        row = (
            await self._session.execute(
                pg_insert(t)
                .values(
                    id=uuid.uuid4(),
                    tenant_id=new.tenant_id,
                    merchant_id=new.merchant_id,
                    environment=new.environment.value,
                    provider=new.provider,
                    provider_account_key=new.provider_account_key,
                    receiving_account_id=new.receiving_account_id,
                    identity_kind=new.identity_kind.value,
                    identity_value=new.identity_value,
                    dedup_key=new.dedup_key,
                    webhook_tx_id=new.identity_value if by_webhook else None,
                    api_tx_id=None if by_webhook else new.identity_value,
                    bank_reference=new.bank_reference,
                    amount_vnd=new.amount.value,
                    direction=new.direction.value,
                    first_source=new.first_source.value,
                    match_state=MatchState.RECORDED.value,
                )
                .on_conflict_do_nothing(
                    index_elements=[t.c.tenant_id, t.c.environment, t.c.dedup_key]
                )
                .returning(t)
            )
        ).first()
        if row is not None:
            return _view(row), True
        existing = (
            await self._session.execute(
                sa.select(t).where(
                    t.c.tenant_id == new.tenant_id,
                    t.c.environment == new.environment.value,
                    t.c.dedup_key == new.dedup_key,
                )
            )
        ).one()
        return _view(existing), False

    async def get_for_update(
        self, tenant_id: str, environment: Environment, transaction_id: UUID
    ) -> TransactionView | None:
        t = self._tables.provider_transactions
        row = (
            await self._session.execute(
                sa.select(t)
                .where(
                    t.c.tenant_id == tenant_id,
                    t.c.environment == Environment(environment).value,
                    t.c.id == transaction_id,
                )
                .with_for_update()
            )
        ).first()
        return None if row is None else _view(row)

    async def set_match_state(
        self, transaction_id: UUID, expected: MatchState, target: MatchState
    ) -> bool:
        """Compare-and-set on ``match_state``; the caller validates the transition."""
        t = self._tables.provider_transactions
        result = await self._session.execute(
            sa.update(t)
            .where(t.c.id == transaction_id, t.c.match_state == MatchState(expected).value)
            .values(match_state=MatchState(target).value)
        )
        return result.rowcount == 1

    async def get(
        self, tenant_id: str, environment: Environment, transaction_id: UUID
    ) -> TransactionView | None:
        t = self._tables.provider_transactions
        row = (
            await self._session.execute(
                sa.select(t).where(
                    t.c.tenant_id == tenant_id,
                    t.c.environment == Environment(environment).value,
                    t.c.id == transaction_id,
                )
            )
        ).first()
        return None if row is None else _view(row)


def _view(row: sa.Row) -> TransactionView:
    return TransactionView(
        id=row.id,
        tenant_id=row.tenant_id,
        environment=row.environment,
        provider_account_key=row.provider_account_key,
        receiving_account_id=row.receiving_account_id,
        merchant_id=row.merchant_id,
        amount=AmountVnd(row.amount_vnd),
        direction=row.direction,
        match_state=row.match_state,
    )
