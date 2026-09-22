"""A failing host handler rolls the whole outcome back and leaves retryable work; crashes at
each transaction boundary recover through lease expiry; a reclaimed owner never finalizes."""

from __future__ import annotations

import asyncio

import pytest

from fakes.payment_app import App
from payment_module.adapters.sqlalchemy.uow import SqlAlchemyUnitOfWork
from payment_module.application.process_inbox import ProcessStatus
from payment_module.domain.enums import InboxStatus, IntentStatus, MatchState

pytestmark = [pytest.mark.integration, pytest.mark.postgres, pytest.mark.timeout(600)]


async def inbox(app: App):
    [row] = await app.rows(app.tables.webhook_inbox)
    return row


async def intent_status(app: App, intent_id) -> str:
    t = app.tables.payment_intents
    [row] = await app.rows(t, t.c.id == intent_id)
    return row.status


async def assert_settled_once(app: App, intent_id) -> None:
    assert await app.count(app.tables.provider_transactions) == 1
    assert await app.count(app.tables.settlements) == 1
    assert await app.count(app.tables.outbox_events) == 1
    assert len(app.handler.calls) == 1
    assert await intent_status(app, intent_id) == IntentStatus.PAID.value
    assert (await inbox(app)).status == InboxStatus.PROCESSED.value


async def test_handler_failure_rolls_back_and_retries(app: App) -> None:
    created = await app.intent()
    app.handler.failures = 1
    await app.webhook(app.payment(code=created.payment_reference))

    [result] = await app.module.process_inbox.run_batch()
    assert result.reason == InboxStatus.RETRY_WAIT.value
    assert await app.count(app.tables.settlements) == 0
    assert await app.count(app.tables.provider_transactions) == 0
    assert await app.count(app.tables.provider_observations) == 0
    assert await app.count(app.tables.outbox_events) == 0
    assert await intent_status(app, created.intent.id) == IntentStatus.AWAITING_PAYMENT.value
    assert (await inbox(app)).status == InboxStatus.RETRY_WAIT.value

    app.clock.advance(seconds=2)
    [retry] = await app.module.process_inbox.run_batch()
    assert retry.reason == InboxStatus.PROCESSED.value
    await assert_settled_once(app, created.intent.id)


async def test_crash_after_claim_recovers_after_lease_expiry(app: App) -> None:
    created = await app.intent()
    await app.webhook(app.payment(code=created.payment_reference))
    async with app.uow_factory()() as uow:
        [claimed] = await uow.inbox.claim_batch(10, 60, "crashed-worker", app.clock.now())
        await uow.commit()

    assert await app.module.process_inbox.run_batch() == []  # lease still valid
    app.clock.advance(seconds=61)
    [result] = await app.module.process_inbox.run_batch()
    assert result.reason == InboxStatus.PROCESSED.value
    assert (await inbox(app)).lease_generation == claimed.lease_generation + 1
    await assert_settled_once(app, created.intent.id)


class _FailureUpdateFails(SqlAlchemyUnitOfWork):
    """The failure transaction cannot be recorded (the process dies after the rollback)."""

    async def __aenter__(self):
        uow = await super().__aenter__()
        original = uow.inbox.finalize

        async def finalize(inbox_id, lease_generation, status, **kwargs):
            if status == InboxStatus.RETRY_WAIT:
                raise ConnectionError("died before the failure update")
            return await original(inbox_id, lease_generation, status, **kwargs)

        uow.inbox.finalize = finalize  # type: ignore[method-assign]
        return uow


async def test_crash_between_rollback_and_failure_update_recovers(app: App) -> None:
    module = app.build(uow_factory=lambda: _FailureUpdateFails(app.sessions, app.tables))
    created = await app.intent()
    app.handler.failures = 1
    await app.webhook(app.payment(code=created.payment_reference))

    [result] = await module.process_inbox.run_batch()
    assert (result.status, result.reason) == (ProcessStatus.SKIPPED, "failure_not_recorded")
    row = await inbox(app)
    assert row.status == InboxStatus.PROCESSING.value  # left for lease expiry
    assert await app.count(app.tables.settlements) == 0

    app.clock.advance(seconds=61)
    [recovered] = await app.module.process_inbox.run_batch()
    assert recovered.reason == InboxStatus.PROCESSED.value
    await assert_settled_once(app, created.intent.id)


async def test_old_owner_cannot_finalize_after_reclaim(app: App) -> None:
    created = await app.intent()
    await app.webhook(app.payment(code=created.payment_reference))
    async with app.uow_factory()() as uow:
        [stale] = await uow.inbox.claim_batch(10, 60, "slow-worker", app.clock.now())
        await uow.commit()

    app.clock.advance(seconds=61)
    [result] = await app.module.process_inbox.run_batch()
    assert result.reason == InboxStatus.PROCESSED.value

    late = await app.module.process_inbox.process_claimed(stale)
    assert (late.status, late.reason) == (ProcessStatus.SKIPPED, "lease_lost")
    await assert_settled_once(app, created.intent.id)
    assert len(app.observer.outcomes) == 1


async def test_reclaim_cannot_take_a_row_while_its_owner_is_processing(app: App) -> None:
    """The owner's processing transaction holds the row lock, so an expired lease is not
    reclaimed until that transaction ends; the reclaim then sees the row processed."""
    created = await app.intent()
    await app.webhook(app.payment(code=created.payment_reference))
    entered, release = asyncio.Event(), asyncio.Event()
    original = app.handler.on_settled

    async def slow(uow, settled) -> None:
        entered.set()
        await release.wait()
        await original(uow, settled)

    app.handler.on_settled = slow  # type: ignore[method-assign]
    owner = asyncio.create_task(app.module.process_inbox.run_batch())
    await entered.wait()

    app.clock.advance(seconds=61)  # the owner's lease has expired meanwhile
    other = app.build()
    assert await other.process_inbox.run_batch() == []  # row locked: skipped, not reclaimed

    release.set()
    [result] = await owner
    assert result.reason == InboxStatus.PROCESSED.value
    assert await other.process_inbox.run_batch() == []
    await assert_settled_once(app, created.intent.id)


async def test_handler_failure_on_last_attempt_fails_without_settling(app: App) -> None:
    created = await app.intent()
    app.handler.failures = 100
    await app.webhook(app.payment(code=created.payment_reference))
    for _ in range(10):
        await app.module.process_inbox.run_batch()
        app.clock.advance(seconds=301)

    row = await inbox(app)
    assert (row.status, row.attempts) == (InboxStatus.FAILED.value, 10)
    assert await app.count(app.tables.settlements) == 0
    assert await intent_status(app, created.intent.id) == IntentStatus.AWAITING_PAYMENT.value
    assert row.next_attempt_at is None


async def test_fact_state_after_retry_is_settled(app: App) -> None:
    created = await app.intent()
    app.handler.failures = 2
    await app.webhook(app.payment(code=created.payment_reference))
    for _ in range(3):
        await app.module.process_inbox.run_batch()
        app.clock.advance(seconds=10)
    [row] = await app.rows(app.tables.provider_transactions)
    assert row.match_state == MatchState.SETTLED.value
    await assert_settled_once(app, created.intent.id)
