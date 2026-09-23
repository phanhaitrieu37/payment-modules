"""Retention purge: personal data goes, identity, dedup and amounts stay; nothing the worker
still needs is purged, and a purged row is never claimed or requeued."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from fakes.fake_reader import (
    FakeTransactionReader,
    account_ref,
    api_row,
    enable_reconcile,
    page,
    reconcile_module,
)
from fakes.payment_app import App
from payment_module.application.config import PaymentModuleConfig
from payment_module.builder import PaymentModule
from payment_module.domain.enums import (
    Direction,
    EventKeyKind,
    InboxStatus,
    LinkStatus,
    MatchState,
    ObservationSource,
    ReviewCaseStatus,
    ReviewReason,
)

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

RETENTION_DAYS = 30
KEPT_INBOX = ("event_key", "body_sha256", "received_at", "status", "connection_id")
KEPT_OBSERVATION = (
    "source_tx_id",
    "reported_account_key",
    "bank_reference",
    "amount_vnd",
    "direction",
    "code",
    "transaction_id",
)
KEPT_FACT = (
    "dedup_key",
    "identity_value",
    "webhook_tx_id",
    "api_tx_id",
    "bank_reference",
    "amount_vnd",
    "match_state",
)


def retaining(app: App) -> PaymentModule:
    return app.build(
        config=PaymentModuleConfig(worker_owner="worker-test", pii_retention_days=RETENTION_DAYS)
    )


async def processed_payment(app: App, module: PaymentModule) -> uuid.UUID:
    intent = await app.intent()
    raw = app.payment(code=intent.intent.payment_reference, content="thanh toan don hang")
    result = await module.ingest_webhook.execute(app.m1.locator, raw, app.headers(raw))
    await module.process_inbox.run_batch()
    await facts_created_on_test_clock(app)
    return result.inbox_id


async def facts_created_on_test_clock(app: App) -> None:
    """``created_at`` is stamped by the database; age the facts on the test clock instead."""
    tx = app.tables.provider_transactions
    await app.execute(sa.update(tx).values(created_at=app.clock.now()))


async def purge_due_now(app: App) -> None:
    """Make every row due as a retention shorter than the link horizon would have."""
    past = app.clock.now() - timedelta(seconds=1)
    for table in (app.tables.webhook_inbox, app.tables.provider_observations):
        await app.execute(sa.update(table).values(purge_after=past))


async def one(app: App, table: sa.Table, *where: Any) -> sa.Row:
    [row] = await app.rows(table, *where)
    return row


async def inbox_row(app: App, status: InboxStatus, **over: Any) -> uuid.UUID:
    row_id = uuid.uuid4()
    values = {
        "id": row_id,
        "tenant_id": app.m1.tenant_id,
        "connection_id": app.m1.connection_id,
        "event_key": f"provider:{row_id.hex}",
        "event_key_kind": EventKeyKind.PROVIDER_ID.value,
        "body_sha256": "0" * 64,
        "raw_body": b'{"id": 1}',
        "headers": {"content-type": "application/json"},
        "received_at": app.clock.now(),
        "status": status.value,
        "purge_after": app.clock.now() - timedelta(seconds=1),
    } | over
    await app.execute(app.tables.webhook_inbox.insert().values(**values))
    return row_id


async def test_finished_delivery_loses_body_and_headers_after_retention(app: App) -> None:
    module = retaining(app)
    inbox_id = await processed_payment(app, module)
    t = app.tables.webhook_inbox
    before = await one(app, t, t.c.id == inbox_id)
    assert before.status == InboxStatus.PROCESSED.value
    assert before.purge_after == app.clock.now() + timedelta(days=RETENTION_DAYS)
    app.clock.advance(days=RETENTION_DAYS, seconds=1)

    result = await module.purge_expired_payloads.execute()

    after = await one(app, t, t.c.id == inbox_id)
    assert (after.raw_body, after.headers, after.raw_purged_at) == (None, None, app.clock.now())
    assert [getattr(after, c) for c in KEPT_INBOX] == [getattr(before, c) for c in KEPT_INBOX]
    assert (result.inbox, result.observations) == (1, 1)


async def test_observation_loses_memo_and_normalized_but_keeps_identity(app: App) -> None:
    module = retaining(app)
    inbox_id = await processed_payment(app, module)
    o = app.tables.provider_observations
    await app.execute(sa.update(o).values(normalized={"content": "thanh toan don hang"}))
    before = await one(app, o, o.c.inbox_id == inbox_id)
    assert before.memo is not None
    app.clock.advance(days=RETENTION_DAYS, seconds=1)

    await module.purge_expired_payloads.execute()

    after = await one(app, o, o.c.inbox_id == inbox_id)
    assert (after.memo, after.normalized) == (None, None)
    assert [getattr(after, c) for c in KEPT_OBSERVATION] == [
        getattr(before, c) for c in KEPT_OBSERVATION
    ]


async def test_fact_loses_memo_but_keeps_identity_and_amount(app: App) -> None:
    module = retaining(app)
    await processed_payment(app, module)
    tx = app.tables.provider_transactions
    await app.execute(
        sa.update(tx).values(memo="thanh toan", purge_after=app.clock.now() + timedelta(days=1))
    )
    before = await one(app, tx)
    app.clock.advance(days=3)

    result = await module.purge_expired_payloads.execute()

    after = await one(app, tx)
    assert after.memo is None
    assert [getattr(after, c) for c in KEPT_FACT] == [getattr(before, c) for c in KEPT_FACT]
    assert result.transactions == 1
    assert await app.count(app.tables.settlements) == 1


async def test_nothing_is_purged_before_purge_after(app: App) -> None:
    module = retaining(app)
    inbox_id = await processed_payment(app, module)
    app.clock.advance(days=RETENTION_DAYS)  # exactly purge_after: not yet before now

    result = await module.purge_expired_payloads.execute()

    t = app.tables.webhook_inbox
    assert result.total == 0
    assert (await one(app, t, t.c.id == inbox_id)).raw_body is not None


async def test_without_retention_nothing_is_ever_purged(app: App) -> None:
    inbox_id = await processed_payment(app, app.module)
    app.clock.advance(days=3650)

    result = await app.module.purge_expired_payloads.execute()

    t = app.tables.webhook_inbox
    assert result.total == 0
    assert (await one(app, t, t.c.id == inbox_id)).purge_after is None


@pytest.mark.parametrize(
    "status", [InboxStatus.RECEIVED, InboxStatus.RETRY_WAIT, InboxStatus.PROCESSING]
)
async def test_deliveries_the_worker_still_needs_are_never_purged(
    app: App, status: InboxStatus
) -> None:
    inbox_id = await inbox_row(app, status)

    result = await app.module.purge_expired_payloads.execute()

    t = app.tables.webhook_inbox
    row = await one(app, t, t.c.id == inbox_id)
    assert result.inbox == 0
    assert (row.raw_body, row.raw_purged_at) == (b'{"id": 1}', None)


@pytest.mark.parametrize("status", [InboxStatus.FAILED, InboxStatus.QUARANTINED])
async def test_failed_and_quarantined_deliveries_are_purged(app: App, status: InboxStatus) -> None:
    inbox_id = await inbox_row(app, status)

    assert (await app.module.purge_expired_payloads.execute()).inbox == 1

    t = app.tables.webhook_inbox
    row = await one(app, t, t.c.id == inbox_id)
    assert (row.raw_body, row.headers, row.status) == (None, None, status.value)


async def test_purge_works_in_batches_and_counts_each_row(app: App) -> None:
    for _ in range(3):
        await inbox_row(app, InboxStatus.PROCESSED)
    purge = app.module.purge_expired_payloads

    assert [(await purge.execute(limit=2)).inbox for _ in range(3)] == [2, 1, 0]
    assert app.metrics.tags.count(("payloads_purged_total", {"table": "inbox"})) == 3


async def test_purged_delivery_is_never_claimed(app: App) -> None:
    await inbox_row(app, InboxStatus.RECEIVED, raw_body=None, raw_purged_at=app.clock.now())

    assert await app.module.process_inbox.run_batch() == []


async def test_purged_failed_delivery_cannot_be_requeued(app: App) -> None:
    inbox_id = await inbox_row(app, InboxStatus.FAILED)
    await app.module.purge_expired_payloads.execute()

    assert await app.module.requeue_inbox.execute(inbox_id, "ops", "retry") is False

    t = app.tables.webhook_inbox
    assert (await one(app, t, t.c.id == inbox_id)).status == InboxStatus.FAILED.value
    assert await app.module.process_inbox.run_batch() == []


async def test_failed_delivery_with_its_body_can_still_be_requeued(app: App) -> None:
    inbox_id = await inbox_row(app, InboxStatus.FAILED, purge_after=None)

    assert await app.module.requeue_inbox.execute(inbox_id, "ops", "retry") is True


async def test_free_text_of_a_fact_in_review_is_kept_until_it_is_decided(app: App) -> None:
    module = retaining(app)
    raw = app.payment(code=None, content="don hang OLDSHOP-77")
    await module.ingest_webhook.execute(app.m1.locator, raw, app.headers(raw))
    await module.process_inbox.run_batch()
    [case] = await app.rows(app.tables.review_cases)
    assert case.reason == ReviewReason.NO_REFERENCE.value
    app.clock.advance(days=RETENTION_DAYS, seconds=1)

    kept = await module.purge_expired_payloads.execute()

    o = app.tables.provider_observations
    assert (kept.observations, (await one(app, o)).memo) == (0, "don hang OLDSHOP-77")

    # The old code is imported later: the review still finds it in the kept memo.
    await module.import_legacy_reference_profile.execute(7, "ops")
    await module.create_intent.execute(
        app.command(
            prefix_name=None,
            reference_override="OLDSHOP-77",
            reference_profile_version=7,
            expires_at=app.clock.now() + timedelta(minutes=15),
        )
    )
    [result] = await module.rematch_reviews.execute(app.m1.tenant_id, "ops")
    assert result.match_state == MatchState.SETTLED
    await app.execute(
        sa.update(app.tables.provider_transactions).values(
            created_at=app.clock.now() - timedelta(days=RETENTION_DAYS, seconds=1)
        )
    )

    decided = await module.purge_expired_payloads.execute()

    assert (decided.observations, (await one(app, o)).memo) == (1, None)


async def test_fact_memo_waits_while_the_fact_is_in_review(app: App) -> None:
    await processed_payment(app, retaining(app))
    tx = app.tables.provider_transactions
    await app.execute(
        sa.update(tx).values(
            memo="thanh toan",
            match_state=MatchState.IN_REVIEW.value,
            purge_after=app.clock.now() - timedelta(seconds=1),
        )
    )

    assert (await app.module.purge_expired_payloads.execute()).transactions == 0
    assert (await one(app, tx)).memo == "thanh toan"


async def test_observation_without_a_fact_is_never_purged(app: App) -> None:
    o = app.tables.provider_observations
    inbox_id = await inbox_row(app, InboxStatus.PROCESSED, purge_after=None)
    await app.execute(
        o.insert().values(
            id=uuid.uuid4(),
            inbox_id=inbox_id,
            tenant_id=app.m1.tenant_id,
            environment=app.m1.environment.value,
            connection_id=app.m1.connection_id,
            provider="fake",
            source=ObservationSource.WEBHOOK.value,
            source_tx_id="777",
            reported_account_key=f"VCB|{app.m1.account_number}|",
            amount_vnd=150_000,
            direction=Direction.IN.value,
            link_status=LinkStatus.UNLINKED.value,
            observed_at=app.clock.now(),
            memo="SUBABC123",
            purge_after=app.clock.now() - timedelta(seconds=1),
        )
    )

    assert (await app.module.purge_expired_payloads.execute()).observations == 0
    assert (await one(app, o)).memo == "SUBABC123"


async def test_drain_repeats_batches_until_nothing_is_left(app: App) -> None:
    for _ in range(5):
        await inbox_row(app, InboxStatus.PROCESSED)

    assert await app.module.purge_expired_payloads.drain(limit=2) == 5
    assert await app.module.purge_expired_payloads.drain(limit=2) == 0


async def test_drain_stops_after_max_batches(app: App) -> None:
    for _ in range(5):
        await inbox_row(app, InboxStatus.PROCESSED)

    assert await app.module.purge_expired_payloads.drain(limit=2, max_batches=2) == 4


async def settled_by_webhook_with_reference_only_in_memo(
    app: App,
) -> tuple[PaymentModule, FakeTransactionReader, str]:
    await enable_reconcile(app, app.m1)
    reader = FakeTransactionReader()
    module = reconcile_module(
        app,
        reader,
        config=PaymentModuleConfig(worker_owner="worker-test", pii_retention_days=RETENTION_DAYS),
    )
    code = (await app.intent()).payment_reference
    raw = app.payment(code=None, content=code, bank_reference="FT-SHARED-1")
    await module.ingest_webhook.execute(app.m1.locator, raw, app.headers(raw))
    await module.process_inbox.run_batch()
    await facts_created_on_test_clock(app)
    assert await app.count(app.tables.settlements) == 1
    return module, reader, code


def script_same_transfer(reader: FakeTransactionReader, app: App, code: str) -> None:
    row = api_row(
        str(uuid.uuid4()),
        account=app.m1.account_number,
        code=None,
        content=code,
        bank_reference="FT-SHARED-1",
    )
    reader.script(account_ref(app.m1), page(row))


async def test_settled_fact_keeps_its_memo_while_the_other_source_can_still_link(
    app: App,
) -> None:
    module, reader, code = await settled_by_webhook_with_reference_only_in_memo(app)
    await purge_due_now(app)

    purged = await module.purge_expired_payloads.execute()

    o = app.tables.provider_observations
    assert (purged.observations, (await one(app, o)).memo) == (0, code)

    # The API reads the same transfer: it links to the settled fact through the kept memo.
    script_same_transfer(reader, app, code)
    await module.reconcile.execute(app.m1.tenant_id, app.m1.connection_id)
    app.clock.advance(seconds=901)
    await module.reconcile.execute(app.m1.tenant_id, app.m1.connection_id)

    cases = app.tables.review_cases
    assert await app.count(app.tables.provider_transactions) == 1
    assert await app.count(app.tables.settlements) == 1
    assert await app.count(cases, cases.c.status == ReviewCaseStatus.OPEN.value) == 0
    assert await app.count(o, o.c.link_status == LinkStatus.LINKED.value) == 2


async def test_fact_with_both_source_ids_is_purged_inside_the_link_horizon(app: App) -> None:
    module, reader, code = await settled_by_webhook_with_reference_only_in_memo(app)
    script_same_transfer(reader, app, code)
    await module.reconcile.execute(app.m1.tenant_id, app.m1.connection_id)
    [fact] = await app.rows(app.tables.provider_transactions)
    assert fact.webhook_tx_id is not None and fact.api_tx_id is not None
    await purge_due_now(app)

    purged = await module.purge_expired_payloads.execute()

    o = app.tables.provider_observations
    assert purged.observations == 2
    assert [(row.memo, row.normalized) for row in await app.rows(o)] == [(None, None)] * 2


async def test_fact_past_the_link_horizon_is_purged(app: App) -> None:
    module, _, _ = await settled_by_webhook_with_reference_only_in_memo(app)
    await purge_due_now(app)
    horizon = module.config.link_horizon()
    app.clock.advance(seconds=horizon.total_seconds() - 1)

    assert (await module.purge_expired_payloads.execute()).observations == 0

    app.clock.advance(seconds=2)

    assert (await module.purge_expired_payloads.execute()).observations == 1
    assert (await one(app, app.tables.provider_observations)).memo is None


async def test_fact_with_a_recent_sighting_waits_for_the_link_horizon(app: App) -> None:
    module, _, _ = await settled_by_webhook_with_reference_only_in_memo(app)
    await purge_due_now(app)
    o = app.tables.provider_observations
    app.clock.advance(days=3)
    # A later sighting of the fact restarts the horizon even though the fact itself is old.
    await app.execute(sa.update(o).values(observed_at=app.clock.now()))

    assert (await module.purge_expired_payloads.execute()).observations == 0
