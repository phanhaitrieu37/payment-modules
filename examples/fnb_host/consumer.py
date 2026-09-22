"""Option B consumer: a bill is paid by the ``PaymentSettled`` event, exactly once.

The outbox delivers at least once; the receipt row keyed by ``event_id`` makes a redelivery
a no-op. The receipt commits only together with exactly one paid bill: an event for a bill
this host does not have raises, so nothing is recorded and the outbox delivers it again
once the bill is there. Cases "Host async failure" and "Crash recovery"
(validation-and-acceptance.md).
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from payment_module.ports.publisher import OutboxEventView

metadata = sa.MetaData()
table_bills = sa.Table(
    "table_bills",
    metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("amount_vnd", sa.BigInteger(), nullable=False),
    sa.Column("receipt_event_id", sa.Uuid()),
)
bill_receipts = sa.Table(
    "bill_receipts",
    metadata,
    sa.Column("event_id", sa.Uuid(), primary_key=True),
    sa.Column("bill_id", sa.String(64), nullable=False),
    sa.Column("message", sa.Text(), nullable=False),
)


class BillNotFound(LookupError):
    """The event names a bill this host does not have (yet); the delivery is retried."""


class BillConsumer:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def publish(self, event: OutboxEventView) -> None:
        if event.event_type != "PaymentSettled":
            return
        message = json.dumps(dict(event.payload), sort_keys=True)  # what a broker would carry
        bill_id = str(event.payload["host_ref_id"])
        async with self._engine.begin() as db:
            receipt = insert(bill_receipts).values(
                event_id=event.event_id, bill_id=bill_id, message=message
            )
            first = await db.execute(receipt.on_conflict_do_nothing().returning(sa.literal(1)))
            if first.first() is not None:
                paid = sa.update(table_bills).where(table_bills.c.id == bill_id)
                result = await db.execute(paid.values(receipt_event_id=event.event_id))
                if result.rowcount != 1:
                    raise BillNotFound(bill_id)  # rolls the receipt back
