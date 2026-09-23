"""Metrics are best effort: a failing metrics backend never changes a money outcome or an
HTTP answer, and alert counters count a committed event once, not once per attempt."""

from __future__ import annotations

import pytest

from fakes.payment_app import App, RaisingMetrics, RecordingMetrics
from payment_module.application.ingest_webhook import IngestStatus
from payment_module.domain.enums import ConnectionStatus, InboxStatus, MatchState
from payment_module.domain.errors import ConnectionNotFound, WebhookAuthError

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


async def test_raising_sink_does_not_roll_back_a_settlement(app: App) -> None:
    module = app.build(metrics=RaisingMetrics())
    created = await app.intent()
    await module.ingest_webhook.execute(
        app.m1.locator, raw := app.payment(code=created.payment_reference), app.headers(raw)
    )
    [result] = await module.process_inbox.run_batch()

    assert result.reason == InboxStatus.PROCESSED.value
    assert await app.count(app.tables.settlements) == 1


async def test_raising_sink_does_not_break_an_unknown_direction_outcome(app: App) -> None:
    sink = RaisingMetrics()
    module = app.build(metrics=sink)
    raw = app.payment(direction="unknown")
    await module.ingest_webhook.execute(app.m1.locator, raw, app.headers(raw))
    [result] = await module.process_inbox.run_batch()

    assert result.reason == InboxStatus.PROCESSED.value
    [row] = await app.rows(app.tables.provider_transactions)
    assert row.match_state == MatchState.NOT_APPLICABLE.value
    assert sink.counts["direction_unknown_total"] == 1


async def test_raising_sink_keeps_404_and_401(app: App) -> None:
    module = app.build(metrics=RaisingMetrics())
    raw = app.payment()
    with pytest.raises(ConnectionNotFound):
        await module.ingest_webhook.execute("0" * 32, raw, app.headers(raw))
    await app.set_connection_status(app.m2, ConnectionStatus.DISABLED)
    with pytest.raises(ConnectionNotFound):
        await module.ingest_webhook.execute(app.m2.locator, raw, app.headers(raw))
    with pytest.raises(WebhookAuthError):
        await module.ingest_webhook.execute(app.m1.locator, raw, app.headers(raw, secret="x"))


async def test_raising_sink_keeps_the_ack_of_a_stored_quarantine(app: App) -> None:
    module = app.build(metrics=RaisingMetrics())
    result = await module.ingest_webhook.execute(app.m1.locator, b"junk", app.headers(b"junk"))
    assert result.status == IngestStatus.QUARANTINED
    assert await app.count(app.tables.webhook_inbox) == 1


async def test_raising_sink_does_not_turn_a_normalize_failure_into_a_retry(app: App) -> None:
    module = app.build(metrics=RaisingMetrics())
    raw = b'{"id": 5, "amount": "x"}'
    await module.ingest_webhook.execute(app.m1.locator, raw, app.headers(raw))
    [result] = await module.process_inbox.run_batch()
    assert result.reason == InboxStatus.QUARANTINED.value


async def test_tenant_mismatch_is_counted_once_across_a_retry(app: App) -> None:
    class FailsOnce:
        def __init__(self) -> None:
            self.failed = False

        async def on_outcome(self, uow, outcome) -> None:
            if not self.failed:
                self.failed = True
                raise RuntimeError("projection failed once")

    metrics = RecordingMetrics()
    module = app.build(metrics=metrics, outcome_observer=FailsOnce())
    foreign = await app.intent(app.mb)
    raw = app.payment(code=foreign.payment_reference)
    await module.ingest_webhook.execute(app.m1.locator, raw, app.headers(raw))
    [first] = await module.process_inbox.run_batch()
    assert first.reason == InboxStatus.RETRY_WAIT.value
    app.clock.advance(seconds=2)
    [second] = await module.process_inbox.run_batch()

    assert second.reason == InboxStatus.PROCESSED.value
    assert metrics.counts["tenant_mismatch_total"] == 1
