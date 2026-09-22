"""Option A hooks: both write through ``uow.session`` inside the settlement transaction, so
the order is paid in the same commit as the settlement, or neither happens."""

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


class MarkOrderPaid:
    async def on_settled(self, uow, settled: SettlementView) -> None:
        paid = sa.update(orders).where(orders.c.id == settled.host_ref_id).values(status="paid")
        await uow.session.execute(paid)


class RecordLastOutcome:
    async def on_outcome(self, uow, outcome: TransactionOutcomeView) -> None:
        last = sa.update(orders).where(orders.c.intent_id == outcome.intent_id)
        await uow.session.execute(last.values(last_outcome=outcome.match_state.value))
