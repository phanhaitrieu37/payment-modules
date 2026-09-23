"""Option A hooks: both write through ``uow.session`` inside the settlement transaction, so
the order is paid in the same commit as the settlement, or neither happens.

A settlement for an order this host does not have raises: the settlement rolls back and the
delivery is retried instead of committing money that paid nothing."""

from __future__ import annotations

import sqlalchemy as sa

from payment_module.ports.handlers import SettlementView, TransactionOutcomeView

orders = sa.Table(
    "orders",
    sa.MetaData(),
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("amount_vnd", sa.BigInteger(), nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("order_code", sa.String(64), unique=True),
    sa.Column("intent_id", sa.Uuid()),
    sa.Column("last_outcome", sa.String(32)),
)


class OrderNotFound(LookupError):
    """The settlement names an order this host does not have."""


class MarkOrderPaid:
    async def on_settled(self, uow, settled: SettlementView) -> None:
        paid = sa.update(orders).where(orders.c.id == settled.host_ref_id).values(status="paid")
        if (await uow.session.execute(paid)).rowcount != 1:
            raise OrderNotFound(settled.host_ref_id)


class RecordLastOutcome:
    async def on_outcome(self, uow, outcome: TransactionOutcomeView) -> None:
        last = sa.update(orders).where(orders.c.intent_id == outcome.intent_id)
        await uow.session.execute(last.values(last_outcome=outcome.match_state.value))
