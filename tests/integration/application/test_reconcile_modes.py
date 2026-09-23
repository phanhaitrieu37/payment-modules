"""Reconciliation decisions by mode and direction, the grace pass, checkpoints and the
round-robin scheduler."""

from __future__ import annotations

from datetime import timedelta

import pytest
import sqlalchemy as sa

from fakes.fake_reader import (
    API_TOKEN,
    FakeTransactionReader,
    account_ref,
    api_row,
    enable_reconcile,
    page,
    reconcile_module,
)
from fakes.payment_app import App, Scope
from payment_module.domain.enums import (
    ConnectionStatus,
    Direction,
    FirstSource,
    IntentStatus,
    LinkMethod,
    LinkStatus,
    MatchState,
    ReconcileMode,
    ReconciliationRunStatus,
    ReviewReason,
    SettlementOrigin,
)
from payment_module.ports.reader import Page, TransactionReadError

pytestmark = [pytest.mark.integration, pytest.mark.postgres, pytest.mark.timeout(600)]

GRACE = timedelta(seconds=901)
API_ID = "5a1c9e2b-0000-4000-8000-000000000001"


async def setup(app: App, mode: ReconcileMode = ReconcileMode.DETECT_ONLY, **build):
    await enable_reconcile(app, app.m1, mode=mode)
    reader = FakeTransactionReader()
    return reader, reconcile_module(app, reader, **build)


async def reconcile(module, scope: Scope):
    return await module.reconcile.execute(scope.tenant_id, scope.connection_id)


async def fact(app: App) -> sa.Row:
    [row] = await app.rows(app.tables.provider_transactions)
    return row


async def after_grace(app: App, module) -> None:
    await reconcile(module, app.m1)
    app.clock.advance(seconds=GRACE.total_seconds())
    await reconcile(module, app.m1)


async def test_unlinked_observation_is_decided_after_grace_though_the_cursor_moved(
    app: App,
) -> None:
    reader, module = await setup(app)
    ref = account_ref(app.m1)
    reader.script(ref, page(api_row(API_ID, account=app.m1.account_number), next_cursor="2"))

    await reconcile(module, app.m1)
    app.clock.advance(minutes=5)
    await reconcile(module, app.m1)  # reads page 2: nothing new, still within grace
    assert await app.count(app.tables.provider_transactions) == 0
    app.clock.advance(minutes=11)
    result = await reconcile(module, app.m1)  # a new window; the old row is not re-read

    assert [call.cursor for call in reader.calls] == [None, "2", None]
    assert result.counts["created"] == 1
    row = await fact(app)
    assert row.first_source == FirstSource.RECONCILE.value
    assert row.api_tx_id == API_ID
    [obs] = await app.rows(app.tables.provider_observations)
    assert (obs.transaction_id, obs.link_method) == (row.id, LinkMethod.SAME_SOURCE_ID.value)
    assert obs.link_status == LinkStatus.LINKED.value

    app.clock.advance(seconds=GRACE.total_seconds())
    again = await reconcile(module, app.m1)
    assert "created" not in again.counts
    assert await app.count(app.tables.review_cases) == 1
    assert await app.count(app.tables.provider_transactions) == 1


async def test_detect_only_incoming_money_opens_an_unverified_identity_review(app: App) -> None:
    reader, module = await setup(app)
    created = await app.intent(expires_at=app.clock.now() + timedelta(hours=2))
    row = api_row(API_ID, account=app.m1.account_number, code=created.payment_reference)
    reader.script(account_ref(app.m1), page(row))

    await after_grace(app, module)

    assert (await fact(app)).match_state == MatchState.IN_REVIEW.value
    [case] = await app.rows(app.tables.review_cases)
    assert case.reason == ReviewReason.UNVERIFIED_IDENTITY.value
    assert case.candidate_intent_id is None
    assert await app.count(app.tables.settlements) == 0
    t = app.tables.payment_intents
    [intent] = await app.rows(t, t.c.id == created.intent.id)
    assert intent.status == IntentStatus.AWAITING_PAYMENT.value
    assert [r.event_type for r in await app.rows(app.tables.outbox_events)] == [
        "PaymentNeedsReview"
    ]


@pytest.mark.parametrize("mode", [ReconcileMode.DETECT_ONLY, ReconcileMode.AUTO_SETTLE])
@pytest.mark.parametrize("direction", [Direction.OUT, Direction.UNKNOWN])
async def test_outgoing_or_unknown_api_money_is_not_applicable(
    app: App, mode: ReconcileMode, direction: Direction
) -> None:
    reader, module = await setup(app, mode)
    row = api_row(API_ID, account=app.m1.account_number, direction=direction)
    reader.script(account_ref(app.m1), page(row))

    await after_grace(app, module)

    assert (await fact(app)).match_state == MatchState.NOT_APPLICABLE.value
    assert await app.count(app.tables.review_cases) == 0
    assert app.metrics.counts.get("direction_unknown_total", 0) == (direction == Direction.UNKNOWN)


async def test_auto_settle_settles_through_the_matching_chain(app: App) -> None:
    reader, module = await setup(app, ReconcileMode.AUTO_SETTLE)
    created = await app.intent(expires_at=app.clock.now() + timedelta(hours=2))
    row = api_row(API_ID, account=app.m1.account_number, content=f"ck {created.payment_reference}")
    reader.script(account_ref(app.m1), page(row))

    await after_grace(app, module)

    assert (await fact(app)).match_state == MatchState.SETTLED.value
    [settlement] = await app.rows(app.tables.settlements)
    assert settlement.intent_id == created.intent.id
    assert settlement.origin == SettlementOrigin.AUTO.value
    [call] = app.handler.calls
    assert call.intent_id == created.intent.id


async def test_auto_settle_never_settles_an_intent_that_expired_before_the_money_was_seen(
    app: App,
) -> None:
    reader, module = await setup(app, ReconcileMode.AUTO_SETTLE)
    created = await app.intent(expires_at=app.clock.now() + timedelta(minutes=5))
    app.clock.advance(minutes=10)
    row = api_row(API_ID, account=app.m1.account_number, code=created.payment_reference)
    reader.script(account_ref(app.m1), page(row))

    await after_grace(app, module)

    [case] = await app.rows(app.tables.review_cases)
    assert case.reason == ReviewReason.LATE.value
    assert await app.count(app.tables.settlements) == 0


async def test_auto_settle_wrong_amount_goes_to_review(app: App) -> None:
    reader, module = await setup(app, ReconcileMode.AUTO_SETTLE)
    created = await app.intent(expires_at=app.clock.now() + timedelta(hours=2))
    row = api_row(
        API_ID, account=app.m1.account_number, code=created.payment_reference, amount=149_000
    )
    reader.script(account_ref(app.m1), page(row))

    await after_grace(app, module)

    [case] = await app.rows(app.tables.review_cases)
    assert case.reason == ReviewReason.AMOUNT_MISMATCH.value
    assert await app.count(app.tables.settlements) == 0


async def test_failure_after_grace_leaves_the_observation_for_the_next_tick(app: App) -> None:
    reader, module = await setup(app, ReconcileMode.AUTO_SETTLE)
    created = await app.intent(expires_at=app.clock.now() + timedelta(hours=2))
    row = api_row(API_ID, account=app.m1.account_number, code=created.payment_reference)
    reader.script(account_ref(app.m1), page(row))
    app.handler.failures = 1

    await after_grace(app, module)

    assert await app.count(app.tables.provider_transactions) == 0
    [obs] = await app.rows(app.tables.provider_observations)
    assert obs.link_status == LinkStatus.UNLINKED.value
    assert app.metrics.counts["reconcile_grace_failed_total"] == 1

    await reconcile(module, app.m1)

    assert (await fact(app)).match_state == MatchState.SETTLED.value
    assert await app.count(app.tables.settlements) == 1
    assert len(app.handler.calls) == 1


async def test_failed_read_keeps_the_checkpoint(app: App) -> None:
    reader, module = await setup(app)
    ref = account_ref(app.m1)
    reader.script(
        ref,
        page(api_row(API_ID, account=app.m1.account_number), next_cursor="2"),
        TransactionReadError("http_429", status=429, retry_after=1.0),
        page(),
    )

    await reconcile(module, app.m1)
    failed = await reconcile(module, app.m1)
    await reconcile(module, app.m1)

    assert failed.counts["read_failed"] == 1
    assert [call.cursor for call in reader.calls] == [None, "2", "2"]
    assert len({call.window for call in reader.calls}) == 1
    assert {call.credential for call in reader.calls} == {API_TOKEN}
    assert {call.account_ref for call in reader.calls} == {ref}
    t = app.tables.reconciliation_runs
    runs = await app.rows(t)
    assert sorted(run.status for run in runs) == sorted(
        [
            ReconciliationRunStatus.COMPLETED.value,
            ReconciliationRunStatus.FAILED.value,
            ReconciliationRunStatus.COMPLETED.value,
        ]
    )
    [failed_run] = [run for run in runs if run.status == ReconciliationRunStatus.FAILED.value]
    assert failed_run.last_error == "http_429"
    assert app.metrics.counts["reconcile_read_failed_total"] == 1


async def test_completed_run_records_counts_and_the_next_cursor(app: App) -> None:
    reader, module = await setup(app)
    rows = [api_row(f"{API_ID[:-1]}{n}", account=app.m1.account_number) for n in (2, 3)]
    reader.script(account_ref(app.m1), page(*rows, next_cursor="2"))

    await reconcile(module, app.m1)

    [run] = await app.rows(app.tables.reconciliation_runs)
    assert run.status == ReconciliationRunStatus.COMPLETED.value
    assert run.cursor == "2"
    assert run.counts["rows"] == 2
    assert run.counts["unlinked"] == 2
    assert run.counts["account_ref"] == account_ref(app.m1)
    t = app.tables.provider_observations
    assert await app.count(t, t.c.reconciliation_run_id == run.id) == 2


async def test_account_without_a_provider_id_is_never_read(app: App) -> None:
    reader, module = await setup(app)
    t = app.tables.receiving_accounts
    await app.execute(
        sa.update(t).where(t.c.id == app.m1.account_id).values(provider_account_ref=None)
    )

    result = await reconcile(module, app.m1)

    assert reader.calls == []
    assert result.counts["unmapped_accounts"] == 1


async def test_disabled_connection_is_skipped(app: App) -> None:
    reader, module = await setup(app)
    await app.set_connection_status(app.m1, ConnectionStatus.DISABLED)

    result = await reconcile(module, app.m1)

    assert result.reason == "disabled"
    assert reader.calls == []


async def test_scheduler_serves_connections_round_robin(app: App) -> None:
    reader, module = await setup(app)
    await enable_reconcile(app, app.mb)
    order = sorted([app.m1, app.mb], key=lambda scope: scope.connection_id)

    served = [(await module.reconcile_scheduler.tick()).connection_id for _ in range(3)]

    assert served == [order[0].connection_id, order[1].connection_id, order[0].connection_id]
    assert [call.account_ref for call in reader.calls] == [
        account_ref(order[0]),
        account_ref(order[1]),
        account_ref(order[0]),
    ]


async def test_scheduler_ignores_connections_without_an_api_credential(app: App) -> None:
    reader, module = await setup(app)
    await app.set_connection_status(app.m1, ConnectionStatus.DISABLED)

    assert await module.reconcile_scheduler.tick() is None
    assert reader.calls == []


async def test_module_without_readers_has_no_reconciliation(app: App) -> None:
    assert app.module.reconcile is None
    assert app.module.reconcile_scheduler is None


async def test_run_records_the_ids_of_rows_the_reader_could_not_read(app: App) -> None:
    reader, module = await setup(app)
    good = api_row(API_ID, account=app.m1.account_number)
    bad_ids = ("5a1c9e2b-0000-4000-8000-00000000000b", "5a1c9e2b-0000-4000-8000-00000000000c")
    reader.script(
        account_ref(app.m1),
        Page(observations=(good,), next_cursor=None, invalid_rows=2, invalid_ids=bad_ids),
    )

    result = await reconcile(module, app.m1)

    assert result.counts["invalid"] == 2
    [run] = await app.rows(app.tables.reconciliation_runs)
    assert run.counts["invalid_ids"] == list(bad_ids)
    assert run.counts["invalid"] == 2
    assert app.metrics.counts["reconcile_row_invalid_total"] == 2
