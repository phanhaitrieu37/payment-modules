"""Payment intents: creation with idempotency and reference retry, and locked lookups."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Collection, Sequence
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from payment_module.adapters.sqlalchemy.repositories import SqlAlchemyRepository
from payment_module.domain.enums import Environment, IntentStatus
from payment_module.domain.intent import IntentView
from payment_module.domain.money import AmountVnd
from payment_module.ports.unit_of_work import (
    IdempotencyConflict,
    NewPaymentIntent,
    ReferenceSpaceExhausted,
)


class SqlAlchemyIntentRepository(SqlAlchemyRepository):
    async def create(
        self,
        new: NewPaymentIntent,
        generate_reference: Callable[[], str],
        max_attempts: int = 5,
    ) -> tuple[IntentView, bool]:
        """Insert with ``ON CONFLICT DO NOTHING`` so a conflict never aborts the transaction.

        When nothing was inserted, the idempotency key decides: an intent with the same
        request fingerprint is returned, another fingerprint raises
        :class:`IdempotencyConflict`, and no intent means the reference collided, so a new
        one is generated. A concurrent insert of the same key blocks on the unique index
        until the other transaction ends, then reads as "already created". Foreign-key and
        CHECK violations are not swallowed.
        """
        t = self._tables.payment_intents
        intent_id = uuid.uuid4()
        for _ in range(max_attempts):
            statement = (
                pg_insert(t)
                .values(
                    id=intent_id,
                    tenant_id=new.tenant_id,
                    merchant_id=new.merchant_id,
                    environment=new.environment.value,
                    receiving_account_id=new.receiving_account_id,
                    amount_vnd=new.amount.value,
                    beneficiary_snapshot=dict(new.beneficiary_snapshot),
                    payment_reference=generate_reference(),
                    reference_profile_version=new.reference_profile_version,
                    reference_prefix_name=new.reference_prefix_name,
                    host_ref_type=new.host_ref_type,
                    host_ref_id=new.host_ref_id,
                    idempotency_key=new.idempotency_key,
                    request_fingerprint=new.request_fingerprint,
                    expires_at=new.expires_at,
                    status=IntentStatus.AWAITING_PAYMENT.value,
                )
                .on_conflict_do_nothing()
                .returning(t)
            )
            row = (await self._session.execute(statement)).first()
            if row is not None:
                return _view(row), True
            existing = (
                await self._session.execute(
                    sa.select(t).where(
                        *_idempotency(t, new.tenant_id, new.environment, new.idempotency_key)
                    )
                )
            ).first()
            if existing is not None:
                if existing.request_fingerprint != new.request_fingerprint:
                    raise IdempotencyConflict(
                        f"idempotency key {new.idempotency_key!r} was used for another request"
                    )
                return _view(existing), False
        raise ReferenceSpaceExhausted(
            f"{max_attempts} generated references collided with existing intents"
        )

    async def get_by_idempotency_key(
        self, tenant_id: str, environment: Environment, idempotency_key: str
    ) -> IntentView | None:
        t = self._tables.payment_intents
        row = (
            await self._session.execute(
                sa.select(t).where(*_idempotency(t, tenant_id, environment, idempotency_key))
            )
        ).first()
        return None if row is None else _view(row)

    async def get_for_update(self, tenant_id: str, intent_id: UUID) -> IntentView | None:
        t = self._tables.payment_intents
        row = (
            await self._session.execute(
                sa.select(t)
                .where(t.c.tenant_id == tenant_id, t.c.id == intent_id)
                .with_for_update()
            )
        ).first()
        return None if row is None else _view(row)

    async def find_by_references_for_update(
        self, payment_references: Collection[str]
    ) -> Sequence[IntentView]:
        """Project-wide lookup without a tenant filter, locked in ``id`` order."""
        if not payment_references:
            return []
        t = self._tables.payment_intents
        rows = await self._session.execute(
            sa.select(t)
            .where(t.c.payment_reference.in_(list(payment_references)))
            .order_by(t.c.id)
            .with_for_update()
        )
        return [_view(row) for row in rows]


def _idempotency(
    t: sa.Table, tenant_id: str, environment: Environment, idempotency_key: str
) -> tuple[sa.ColumnElement[bool], ...]:
    return (
        t.c.tenant_id == tenant_id,
        t.c.environment == Environment(environment).value,
        t.c.idempotency_key == idempotency_key,
    )


def _view(row: sa.Row) -> IntentView:
    return IntentView(
        id=row.id,
        tenant_id=row.tenant_id,
        merchant_id=row.merchant_id,
        environment=row.environment,
        receiving_account_id=row.receiving_account_id,
        amount=AmountVnd(row.amount_vnd),
        status=row.status,
        payment_reference=row.payment_reference,
        expires_at=row.expires_at,
        superseded_by_intent_id=row.superseded_by_intent_id,
    )
