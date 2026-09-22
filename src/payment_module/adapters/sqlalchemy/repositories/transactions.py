"""Canonical provider transactions (one row per movement of money)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from payment_module.adapters.sqlalchemy.repositories import SqlAlchemyRepository
from payment_module.domain.enums import Direction, Environment, IdentityKind, MatchState
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
                    occurred_at=new.occurred_at,
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

    async def lock_match_key(
        self,
        tenant_id: str,
        environment: Environment,
        provider: str,
        provider_account_key: str,
        bank_reference: str,
    ) -> None:
        """Serialize link-or-create for one scoped bank reference until the transaction ends.

        A transaction-scoped advisory lock: webhook and reconciliation paths that may link
        the same money take it before reading, so they cannot both create a fact. The lock
        is not proof of identity; ``bank_reference`` is not unique.
        """
        key = "|".join(
            (
                tenant_id,
                Environment(environment).value,
                provider,
                provider_account_key,
                bank_reference,
            )
        )
        await self._session.execute(
            sa.select(sa.func.pg_advisory_xact_lock(sa.func.hashtextextended(key, 0)))
        )

    async def find_by_source_id(
        self,
        tenant_id: str,
        environment: Environment,
        provider: str,
        provider_account_key: str,
        identity_kind: IdentityKind,
        value: str,
    ) -> TransactionView | None:
        """The fact that already carries this webhook or API id, locked ``FOR UPDATE``."""
        t = self._tables.provider_transactions
        column = _source_column(t, identity_kind)
        row = (
            await self._session.execute(
                sa.select(t)
                .where(
                    t.c.tenant_id == tenant_id,
                    t.c.environment == Environment(environment).value,
                    t.c.provider == provider,
                    t.c.provider_account_key == provider_account_key,
                    column == value,
                )
                .with_for_update()
            )
        ).first()
        return None if row is None else _view(row)

    async def find_linkable_by_bank_reference(
        self,
        tenant_id: str,
        environment: Environment,
        provider: str,
        provider_account_key: str,
        bank_reference: str,
        direction: Direction,
        amount: AmountVnd,
        *,
        missing: IdentityKind,
        created_after: datetime | None = None,
    ) -> Sequence[TransactionView]:
        """Facts of the same scope, bank reference, direction and amount that still lack an
        id of kind ``missing``, locked ``FOR UPDATE`` in ``id`` order.

        ``created_after`` bounds how old a candidate may be when the provider time of the
        money is not trusted.
        """
        t = self._tables.provider_transactions
        query = (
            sa.select(t)
            .where(
                t.c.tenant_id == tenant_id,
                t.c.environment == Environment(environment).value,
                t.c.provider == provider,
                t.c.provider_account_key == provider_account_key,
                t.c.bank_reference == bank_reference,
                t.c.direction == Direction(direction).value,
                t.c.amount_vnd == amount.value,
                _source_column(t, missing).is_(None),
            )
            .order_by(t.c.id)
            .with_for_update()
        )
        if created_after is not None:
            query = query.where(t.c.created_at >= created_after)
        return [_view(row) for row in await self._session.execute(query)]

    async def attach_source_id(
        self, transaction_id: UUID, identity_kind: IdentityKind, value: str
    ) -> bool:
        """Record the other source's id on a fact; ``False`` when it already has one."""
        t = self._tables.provider_transactions
        column = _source_column(t, identity_kind)
        result = await self._session.execute(
            sa.update(t)
            .where(t.c.id == transaction_id, column.is_(None))
            .values({column.name: value})
        )
        return result.rowcount == 1

    async def set_receiver(
        self, transaction_id: UUID, merchant_id: UUID, receiving_account_id: UUID
    ) -> bool:
        """Give a fact in review that has no receiver the one resolved after a binding.

        The account foreign key keeps the receiver in the fact's tenant and environment.
        """
        t = self._tables.provider_transactions
        result = await self._session.execute(
            sa.update(t)
            .where(
                t.c.id == transaction_id,
                t.c.receiving_account_id.is_(None),
                t.c.match_state == MatchState.IN_REVIEW.value,
            )
            .values(receiving_account_id=receiving_account_id, merchant_id=merchant_id)
        )
        return result.rowcount == 1

    async def mark_duplicate_of(
        self, transaction_id: UUID, duplicate_of_transaction_id: UUID
    ) -> bool:
        """``in_review -> duplicate_of`` by compare-and-set, recording the original.

        The self foreign key keeps the original in the same tenant and environment.
        """
        t = self._tables.provider_transactions
        result = await self._session.execute(
            sa.update(t)
            .where(t.c.id == transaction_id, t.c.match_state == MatchState.IN_REVIEW.value)
            .values(
                match_state=MatchState.DUPLICATE_OF.value,
                duplicate_of_transaction_id=duplicate_of_transaction_id,
            )
        )
        return result.rowcount == 1


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
        bank_reference=row.bank_reference,
        webhook_tx_id=row.webhook_tx_id,
        api_tx_id=row.api_tx_id,
        occurred_at=row.occurred_at,
    )


def _source_column(t: sa.Table, identity_kind: IdentityKind) -> sa.Column[str]:
    if IdentityKind(identity_kind) == IdentityKind.WEBHOOK_ID:
        return t.c.webhook_tx_id
    return t.c.api_tx_id
