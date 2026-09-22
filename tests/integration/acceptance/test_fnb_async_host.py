"""Cases "Host async failure" and "Crash recovery" on the option B example host.

The ``fnb_host`` code runs in-process on a database of its own. Fulfilment failing or
dying never undoes ``paid`` (invariant 9); the outbox redelivers and the consumer's receipt
keyed by ``event_id`` keeps the bill paid exactly once.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from payment_module.builder import PaymentModule
from payment_module.domain.enums import IntentStatus, OutboxStatus
from payment_module.ports.publisher import OutboxEventView

pytestmark = [pytest.mark.integration, pytest.mark.postgres, pytest.mark.acceptance]


class ShiftedClock:
    """Wall-clock time plus an offset: SePay signatures carry the real time."""

    def __init__(self) -> None:
        self.offset = timedelta()

    def now(self) -> datetime:
        return datetime.now(UTC) + self.offset


class ScriptedPublisher:
    """In front of the host consumer: fails ``failures`` times, or hangs once after the
    consumer committed (a dispatcher killed before it could record the publish)."""

    def __init__(self, consumer: Any) -> None:
        self.consumer = consumer
        self.failures = 0
        self.hang_after_delivery = False
        self.deliveries = 0

    async def publish(self, event: OutboxEventView) -> None:
        if self.failures > 0:
            self.failures -= 1
            raise ConnectionError("consumer unavailable")
        await self.consumer.publish(event)
        self.deliveries += 1
        if self.hang_after_delivery:
            self.hang_after_delivery = False
            await asyncio.Event().wait()


@dataclass
class FnbHost:
    main: ModuleType
    consumer: ModuleType
    engine: AsyncEngine
    module: PaymentModule
    publisher: ScriptedPublisher
    clock: ShiftedClock
    scope: dict[str, Any]
    locator: str

    async def paid_bill(self, bill_id: str, amount_vnd: int) -> Any:
        """Open a bill, pay it with a signed delivery and process the inbox."""
        from _shared.sign_webhook import signed_delivery

        bill = await self.main.open_bill(
            self.module, self.engine, bill_id, amount_vnd, **self.scope
        )
        code = bill.intent.payment_reference
        secret = os.environ["SEPAY_WEBHOOK_SECRET"]
        raw, headers = signed_delivery(secret, self.main.ACCOUNT, amount_vnd, code, code)
        await self.module.ingest_webhook.execute(self.locator, raw, headers)
        await self.module.process_inbox.run_batch()
        return bill

    async def rows(self, table: sa.Table) -> list[sa.Row]:
        async with self.engine.connect() as db:
            return list((await db.execute(sa.select(table))).all())

    async def intent_status(self, bill: Any) -> IntentStatus:
        view = await self.module.get_intent_status.execute(self.main.TENANT, bill.intent.id)
        return view.effective_status


@pytest.fixture
async def fnb(
    create_database, examples_on_path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[FnbHost]:
    import fnb_host.consumer as consumer
    import fnb_host.main as main

    monkeypatch.setenv("SEPAY_WEBHOOK_SECRET", "whsec-fnb-test")
    engine = create_async_engine(await create_database("fnb"))
    try:
        async with engine.begin() as db:
            await db.run_sync(consumer.metadata.create_all)
        publisher, clock = ScriptedPublisher(consumer.BillConsumer(engine)), ShiftedClock()
        module = main.build(engine, publisher, clock)
        connection, account_id = await main.onboard(module)
        scope = {"merchant_id": connection.merchant_id, "receiving_account_id": account_id}
        yield FnbHost(main, consumer, engine, module, publisher, clock, scope, connection.locator)
    finally:
        await engine.dispose()


async def test_consumer_failure_keeps_the_intent_paid_and_the_retry_pays_the_bill_once(
    fnb: FnbHost,
) -> None:
    fnb.publisher.failures = 1
    bill = await fnb.paid_bill("table-1", 180_000)

    [failed] = await fnb.module.dispatch_outbox.run_batch()

    assert failed.status == OutboxStatus.PENDING
    assert await fnb.intent_status(bill) == IntentStatus.PAID
    assert await fnb.rows(fnb.consumer.bill_receipts) == []

    fnb.clock.offset += timedelta(seconds=5)  # past the first retry delay
    [retried] = await fnb.module.dispatch_outbox.run_batch()

    assert retried.status == OutboxStatus.PUBLISHED
    [receipt] = await fnb.rows(fnb.consumer.bill_receipts)
    [paid] = await fnb.rows(fnb.consumer.table_bills)
    assert (paid.id, paid.receipt_event_id) == ("table-1", receipt.event_id)
    assert await fnb.module.dispatch_outbox.run_batch() == []


async def test_dispatcher_killed_after_the_consumer_committed_redelivers_harmlessly(
    fnb: FnbHost,
) -> None:
    bill = await fnb.paid_bill("table-2", 240_000)
    fnb.publisher.hang_after_delivery = True

    dispatcher = asyncio.create_task(fnb.module.dispatch_outbox.run_batch())
    while fnb.publisher.deliveries == 0:
        await asyncio.sleep(0.01)
    dispatcher.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await dispatcher
    [outbox] = await fnb.rows(fnb.main.TABLES.outbox_events)
    assert (outbox.status, outbox.lease_until is not None) == (OutboxStatus.PENDING.value, True)

    fnb.clock.offset += timedelta(seconds=fnb.module.config.lease_seconds + 1)
    [redelivered] = await fnb.module.dispatch_outbox.run_batch()

    assert redelivered.status == OutboxStatus.PUBLISHED
    assert fnb.publisher.deliveries == 2
    [receipt] = await fnb.rows(fnb.consumer.bill_receipts)
    [paid] = await fnb.rows(fnb.consumer.table_bills)
    assert paid.receipt_event_id == receipt.event_id == redelivered.event_id
    assert await fnb.intent_status(bill) == IntentStatus.PAID


async def test_the_consumer_ignores_a_repeated_event(fnb: FnbHost) -> None:
    await fnb.paid_bill("table-3", 90_000)
    await fnb.module.dispatch_outbox.run_batch()
    [outbox] = await fnb.rows(fnb.main.TABLES.outbox_events)
    event = OutboxEventView(
        outbox.event_id,
        outbox.event_type,
        outbox.schema_version,
        outbox.trusted_scope,
        outbox.payload,
        outbox.occurred_at,
    )

    await fnb.publisher.consumer.publish(event)

    assert len(await fnb.rows(fnb.consumer.bill_receipts)) == 1
    assert event.payload["event_type"] == "PaymentSettled"
    assert event.payload["schema_version"] == 1
