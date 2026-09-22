"""Outbox delivery: at least once, backoff, failure, requeue, lease fencing, and replay after
a crash between commit and publish; plus the builder wiring."""

from __future__ import annotations

from datetime import timedelta

import pytest

from fakes.payment_app import App
from payment_module.application.config import PaymentModuleConfig
from payment_module.application.create_intent import CreateIntent
from payment_module.application.ingest_webhook import IngestWebhook
from payment_module.application.process_inbox import ProcessInbox
from payment_module.domain.enums import OutboxStatus

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


async def settled_event(app: App) -> None:
    created = await app.intent()
    await app.pay(code=created.payment_reference)


async def outbox_row(app: App):
    [row] = await app.rows(app.tables.outbox_events)
    return row


async def test_pending_event_is_published_once(app: App) -> None:
    await settled_event(app)
    [result] = await app.module.dispatch_outbox.run_batch()

    assert result.status == OutboxStatus.PUBLISHED
    row = await outbox_row(app)
    assert (row.status, row.published_at) == (OutboxStatus.PUBLISHED.value, app.clock.now())
    [event] = app.publisher.delivered
    assert event.event_type == "PaymentSettled" and event.schema_version == 1
    assert await app.module.dispatch_outbox.run_batch() == []


async def test_publish_failure_backs_off_then_fails_and_can_be_requeued(app: App) -> None:
    module = app.build(config=PaymentModuleConfig(worker_owner="w", outbox_max_attempts=2))
    await settled_event(app)
    app.publisher.failures = 5

    [first] = await module.dispatch_outbox.run_batch()
    assert first.status == OutboxStatus.PENDING
    row = await outbox_row(app)
    assert row.next_attempt_at == app.clock.now() + timedelta(seconds=2)
    assert row.last_error == "ConnectionError"
    assert await module.dispatch_outbox.run_batch() == []

    app.clock.advance(seconds=2)
    [second] = await module.dispatch_outbox.run_batch()
    assert second.status == OutboxStatus.FAILED
    assert await module.dispatch_outbox.run_batch() == []

    event_id = (await outbox_row(app)).event_id
    assert await module.requeue_outbox.execute(event_id, "ops@host", "broker back")
    app.publisher.failures = 0
    [third] = await module.dispatch_outbox.run_batch()
    assert third.status == OutboxStatus.PUBLISHED
    assert not await module.requeue_outbox.execute(event_id, "ops@host", "again")


async def test_crash_between_commit_and_publish_replays_the_same_event(app: App) -> None:
    """The settlement committed with its event; the dispatcher dies after claiming it."""
    await settled_event(app)
    async with app.uow_factory()() as uow:
        [claimed] = await uow.outbox.claim_batch(10, 60, "dead-worker", app.clock.now())
        await uow.commit()

    assert await app.module.dispatch_outbox.run_batch() == []  # lease still held
    app.clock.advance(seconds=61)
    [result] = await app.module.dispatch_outbox.run_batch()
    assert result.status == OutboxStatus.PUBLISHED
    assert [e.event_id for e in app.publisher.delivered] == [claimed.event.event_id]

    # The dead worker comes back and delivers too: at least once. Its result is fenced out.
    await app.publisher.publish(claimed.event)
    async with app.uow_factory()() as uow:
        recorded = await uow.outbox.finalize(
            claimed.event.event_id,
            claimed.lease_generation,
            OutboxStatus.FAILED,
            now=app.clock.now(),
        )
        await uow.commit()
    assert not recorded
    assert (await outbox_row(app)).status == OutboxStatus.PUBLISHED.value

    # An idempotent consumer applies each event id once.
    applied = {event.event_id for event in app.publisher.delivered}
    assert len(app.publisher.delivered) == 2 and len(applied) == 1
    assert await app.count(app.tables.settlements) == 1


async def test_builder_wires_every_use_case(app: App) -> None:
    module = app.build(outbox_publisher=None)
    assert module.dispatch_outbox is None
    assert isinstance(module.create_intent, CreateIntent)
    assert isinstance(module.ingest_webhook, IngestWebhook)
    assert isinstance(module.process_inbox, ProcessInbox)
    assert module.providers == {"fake": app.provider}
    assert app.module.dispatch_outbox is not None
