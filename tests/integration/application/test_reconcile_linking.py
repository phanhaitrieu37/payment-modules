"""Linking API and webhook sightings of the same money: one fact when identity is proven,
separate facts (and review) when it is not."""

from __future__ import annotations

import asyncio
from datetime import timedelta

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
from fakes.payment_app import App, Scope
from payment_module.domain.enums import (
    FirstSource,
    LinkMethod,
    LinkStatus,
    MatchState,
    ObservationSource,
    ReconcileMode,
    ReviewCaseStatus,
    ReviewReason,
    ReviewResolution,
    SettlementOrigin,
)

pytestmark = [pytest.mark.integration, pytest.mark.postgres, pytest.mark.timeout(600)]

MODES = [ReconcileMode.DETECT_ONLY, ReconcileMode.AUTO_SETTLE]
GRACE = timedelta(seconds=901)
API_ID = "0b3f6c1e-8d2a-4c1e-9f57-2d4a6b8c9e01"


async def setup(app: App, mode: ReconcileMode, scope: Scope | None = None):
    scope = scope or app.m1
    await enable_reconcile(app, scope, mode=mode)
    reader = FakeTransactionReader()
    return reader, reconcile_module(app, reader)


async def facts(app: App) -> list[sa.Row]:
    t = app.tables.provider_transactions
    return list(await app.rows(t))


async def observations(app: App, source: ObservationSource) -> list[sa.Row]:
    t = app.tables.provider_observations
    return await app.rows(t, t.c.source == source.value)


async def reconcile(module, scope: Scope):
    return await module.reconcile.execute(scope.tenant_id, scope.connection_id)


@pytest.mark.parametrize("mode", MODES)
async def test_webhook_then_api_links_to_the_webhook_fact(app: App, mode: ReconcileMode) -> None:
    reader, module = await setup(app, mode)
    created = await app.intent(expires_at=app.clock.now() + timedelta(hours=2))
    await app.pay(code=created.payment_reference, bank_reference="FT24012345678")
    reader.script(
        account_ref(app.m1),
        page(
            api_row(
                API_ID,
                account=app.m1.account_number,
                code=created.payment_reference,
                bank_reference="FT24012345678",
            )
        ),
    )

    result = await reconcile(module, app.m1)

    assert result.counts["linked"] == 1
    [fact] = await facts(app)
    assert fact.match_state == MatchState.SETTLED.value
    assert fact.api_tx_id == API_ID
    [api] = await observations(app, ObservationSource.API)
    assert api.transaction_id == fact.id
    assert api.link_method == LinkMethod.BANK_REFERENCE.value
    assert api.link_status == LinkStatus.LINKED.value
    assert await app.count(app.tables.settlements) == 1

    app.clock.advance(seconds=GRACE.total_seconds())
    await reconcile(module, app.m1)
    assert len(await facts(app)) == 1
    assert await app.count(app.tables.settlements) == 1
    assert await app.count(app.tables.review_cases) == 0


async def test_api_then_webhook_in_detect_only_reviews_then_settles_one_fact(app: App) -> None:
    reader, module = await setup(app, ReconcileMode.DETECT_ONLY)
    created = await app.intent(expires_at=app.clock.now() + timedelta(hours=2))
    row = api_row(API_ID, account=app.m1.account_number, code=created.payment_reference)
    reader.script(account_ref(app.m1), page(row))

    await reconcile(module, app.m1)
    assert await facts(app) == []
    app.clock.advance(seconds=GRACE.total_seconds())
    await reconcile(module, app.m1)

    [fact] = await facts(app)
    assert fact.first_source == FirstSource.RECONCILE.value
    assert fact.match_state == MatchState.IN_REVIEW.value
    [case] = await app.rows(app.tables.review_cases)
    assert case.reason == ReviewReason.UNVERIFIED_IDENTITY.value
    assert await app.count(app.tables.settlements) == 0

    # The signed webhook for the same money arrives late: it joins the API fact.
    await app.pay(code=created.payment_reference)

    [fact] = await facts(app)
    assert fact.match_state == MatchState.SETTLED.value
    assert fact.webhook_tx_id is not None
    [webhook] = await observations(app, ObservationSource.WEBHOOK)
    assert webhook.transaction_id == fact.id
    assert webhook.link_method == LinkMethod.BANK_REFERENCE.value
    [settlement] = await app.rows(app.tables.settlements)
    assert settlement.intent_id == created.intent.id
    # The API-only review closes with the settlement, in the same transaction.
    [case] = await app.rows(app.tables.review_cases)
    assert case.status == ReviewCaseStatus.RESOLVED.value
    assert case.resolution == ReviewResolution.SETTLED_BY_WEBHOOK.value
    assert case.resolved_by == "system"
    assert settlement.review_case_id == case.id
    assert settlement.origin == SettlementOrigin.AUTO.value
    events = {row.event_type: row for row in await app.rows(app.tables.outbox_events)}
    assert set(events) == {"PaymentNeedsReview", "PaymentSettled", "ReviewResolved"}
    resolved = events["ReviewResolved"].payload
    assert resolved["review_case_id"] == str(case.id)
    assert resolved["settlement_id"] == str(settlement.id)
    assert resolved["resolution"] == ReviewResolution.SETTLED_BY_WEBHOOK.value


async def test_api_then_webhook_in_auto_settle_settles_once(app: App) -> None:
    reader, module = await setup(app, ReconcileMode.AUTO_SETTLE)
    created = await app.intent(expires_at=app.clock.now() + timedelta(hours=2))
    row = api_row(API_ID, account=app.m1.account_number, code=created.payment_reference)
    reader.script(account_ref(app.m1), page(row))

    await reconcile(module, app.m1)
    app.clock.advance(seconds=GRACE.total_seconds())
    await reconcile(module, app.m1)
    [fact] = await facts(app)
    assert fact.match_state == MatchState.SETTLED.value

    await app.pay(code=created.payment_reference)

    [fact] = await facts(app)
    assert fact.webhook_tx_id is not None
    assert await app.count(app.tables.settlements) == 1
    assert len(app.handler.calls) == 1


@pytest.mark.parametrize("mode", MODES)
async def test_webhook_during_grace_creates_the_fact_and_the_api_sighting_joins_it(
    app: App, mode: ReconcileMode
) -> None:
    reader, module = await setup(app, mode)
    created = await app.intent(expires_at=app.clock.now() + timedelta(hours=2))
    row = api_row(API_ID, account=app.m1.account_number, code=created.payment_reference)
    reader.script(account_ref(app.m1), page(row))

    await reconcile(module, app.m1)
    [api] = await observations(app, ObservationSource.API)
    assert api.link_status == LinkStatus.UNLINKED.value

    app.clock.advance(minutes=5)
    await app.pay(code=created.payment_reference)
    app.clock.advance(seconds=GRACE.total_seconds())
    await reconcile(module, app.m1)

    [fact] = await facts(app)
    assert fact.first_source == FirstSource.WEBHOOK.value
    assert fact.match_state == MatchState.SETTLED.value
    assert fact.api_tx_id == API_ID
    [api] = await observations(app, ObservationSource.API)
    assert (api.transaction_id, api.link_method) == (fact.id, LinkMethod.BANK_REFERENCE.value)
    assert await app.count(app.tables.settlements) == 1
    assert await app.count(app.tables.review_cases) == 0


@pytest.mark.parametrize("mode", MODES)
async def test_webhook_x3_reconcile_x2_single_settlement(app: App, mode: ReconcileMode) -> None:
    reader, module = await setup(app, mode)
    created = await app.intent(expires_at=app.clock.now() + timedelta(hours=2))
    raw = app.payment(code=created.payment_reference)
    for _ in range(3):
        await app.webhook(raw)
        await app.module.process_inbox.run_batch()
    row = api_row(API_ID, account=app.m1.account_number, code=created.payment_reference)
    reader.script(account_ref(app.m1), page(row), page(row))
    for _ in range(2):
        await reconcile(module, app.m1)
        app.clock.advance(seconds=GRACE.total_seconds())

    t = app.tables.provider_observations
    assert await app.count(t, t.c.source == ObservationSource.WEBHOOK.value) == 1
    assert await app.count(t, t.c.source == ObservationSource.API.value) == 1
    assert await app.count(app.tables.provider_transactions) == 1
    assert await app.count(app.tables.settlements) == 1
    assert await app.count(app.tables.review_cases) == 0


async def test_distinct_transfers_with_the_same_reference_are_never_merged(app: App) -> None:
    reader, module = await setup(app, ReconcileMode.AUTO_SETTLE)
    created = await app.intent(expires_at=app.clock.now() + timedelta(hours=2))
    # Two real transfers: same amount, memo and bank reference, different webhook ids.
    await app.pay(code=created.payment_reference)
    await app.pay(code=created.payment_reference)
    reader.script(
        account_ref(app.m1),
        page(api_row(API_ID, account=app.m1.account_number, code=created.payment_reference)),
    )

    result = await reconcile(module, app.m1)
    assert result.counts["ambiguous"] == 1
    [api] = await observations(app, ObservationSource.API)
    assert api.link_status == LinkStatus.AMBIGUOUS.value
    assert api.transaction_id is None
    app.clock.advance(seconds=GRACE.total_seconds())
    await reconcile(module, app.m1)

    # Neither webhook fact was merged with the API sighting, which got its own fact.
    webhook_facts = [f for f in await facts(app) if f.first_source == FirstSource.WEBHOOK.value]
    assert len(webhook_facts) == 2
    assert all(fact.api_tx_id is None for fact in webhook_facts)
    assert app.metrics.counts["reconcile_ambiguous_total"] == 1
    # One intent, one settlement; the second transfer and the API sighting wait for review.
    assert await app.count(app.tables.settlements) == 1
    reasons = sorted(case.reason for case in await app.rows(app.tables.review_cases))
    assert reasons == [ReviewReason.ALREADY_PAID.value, ReviewReason.UNVERIFIED_IDENTITY.value]


async def test_two_api_only_transfers_with_the_same_reference_stay_separate(app: App) -> None:
    reader, module = await setup(app, ReconcileMode.AUTO_SETTLE)
    created = await app.intent(expires_at=app.clock.now() + timedelta(hours=2))
    rows = [
        api_row(f"{API_ID[:-2]}0{n}", account=app.m1.account_number, code=created.payment_reference)
        for n in (1, 2)
    ]
    reader.script(account_ref(app.m1), page(*rows))

    await reconcile(module, app.m1)
    app.clock.advance(seconds=GRACE.total_seconds())
    await reconcile(module, app.m1)

    assert sorted(fact.api_tx_id for fact in await facts(app)) == [row.source_tx_id for row in rows]
    assert await app.count(app.tables.settlements) == 1
    [case] = await app.rows(app.tables.review_cases)
    assert case.reason == ReviewReason.ALREADY_PAID.value


async def test_api_id_already_on_another_fact_is_never_overwritten(app: App) -> None:
    reader, module = await setup(app, ReconcileMode.DETECT_ONLY)
    created = await app.intent(expires_at=app.clock.now() + timedelta(hours=2))
    await app.pay(code=created.payment_reference)
    other_id = f"{API_ID[:-2]}99"
    reader.script(
        account_ref(app.m1),
        page(api_row(API_ID, account=app.m1.account_number, code=created.payment_reference)),
        page(api_row(other_id, account=app.m1.account_number, code=created.payment_reference)),
    )

    await reconcile(module, app.m1)
    await reconcile(module, app.m1)
    app.clock.advance(seconds=GRACE.total_seconds())
    await reconcile(module, app.m1)

    by_source = {fact.first_source: fact for fact in await facts(app)}
    assert by_source[FirstSource.WEBHOOK.value].api_tx_id == API_ID
    assert by_source[FirstSource.RECONCILE.value].api_tx_id == other_id
    assert by_source[FirstSource.RECONCILE.value].match_state == MatchState.IN_REVIEW.value
    assert await app.count(app.tables.settlements) == 1


async def test_same_reference_in_another_environment_or_account_never_links(app: App) -> None:
    await enable_reconcile(app, app.m1_live)
    await enable_reconcile(app, app.m1)
    reader = FakeTransactionReader()
    module = reconcile_module(app, reader)
    created = await app.intent(expires_at=app.clock.now() + timedelta(hours=2))
    await app.pay(code=created.payment_reference)
    reader.script(
        account_ref(app.m1_live), page(api_row(API_ID, account=app.m1_live.account_number))
    )
    reader.script(
        account_ref(app.m1), page(api_row(f"{API_ID[:-2]}77", account=app.m2.account_number))
    )

    await reconcile(module, app.m1_live)
    await reconcile(module, app.m1)
    app.clock.advance(seconds=GRACE.total_seconds())
    await reconcile(module, app.m1_live)
    await reconcile(module, app.m1)

    webhook_fact = next(
        fact for fact in await facts(app) if fact.first_source == FirstSource.WEBHOOK.value
    )
    assert webhook_fact.api_tx_id is None
    assert len(await facts(app)) == 3
    t = app.tables.provider_observations
    linked = await app.rows(t, t.c.link_method == LinkMethod.BANK_REFERENCE.value)
    assert linked == []
    assert await app.count(app.tables.settlements) == 1


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("api_first", [True, False])
async def test_concurrent_webhook_and_api_make_one_fact(
    app: App, mode: ReconcileMode, api_first: bool
) -> None:
    reader, module = await setup(app, mode)
    created = await app.intent(expires_at=app.clock.now() + timedelta(hours=2))
    raw = app.payment(code=created.payment_reference)
    await app.webhook(raw)
    row = api_row(API_ID, account=app.m1.account_number, code=created.payment_reference)
    reader.script(account_ref(app.m1), page(row))
    reader.barrier = asyncio.Barrier(2)

    async def webhook_worker() -> None:
        await reader.barrier.wait()
        if api_first:
            await asyncio.sleep(0)
        await module.process_inbox.run_batch()

    async def api_worker() -> None:
        if not api_first:
            await asyncio.sleep(0)
        await reconcile(module, app.m1)

    await asyncio.gather(api_worker(), webhook_worker())
    reader.barrier = None
    app.clock.advance(seconds=GRACE.total_seconds())
    await reconcile(module, app.m1)

    [fact] = await facts(app)
    assert fact.match_state == MatchState.SETTLED.value
    assert fact.webhook_tx_id is not None and fact.api_tx_id == API_ID
    [api] = await observations(app, ObservationSource.API)
    assert api.transaction_id == fact.id
    assert await app.count(app.tables.settlements) == 1


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        (ReconcileMode.DETECT_ONLY, ReviewReason.UNVERIFIED_IDENTITY),
        (ReconcileMode.AUTO_SETTLE, ReviewReason.NO_REFERENCE),
    ],
)
async def test_same_reference_and_amount_but_different_code_is_not_linked(
    app: App, mode: ReconcileMode, reason: ReviewReason
) -> None:
    """The negative twin of the single-settlement test: only the reference tokens differ."""
    reader, module = await setup(app, mode)
    created = await app.intent(expires_at=app.clock.now() + timedelta(hours=2))
    await app.pay(code=created.payment_reference)
    reader.script(
        account_ref(app.m1), page(api_row(API_ID, account=app.m1.account_number, code="XYZ12345"))
    )

    await reconcile(module, app.m1)
    app.clock.advance(seconds=GRACE.total_seconds())
    await reconcile(module, app.m1)

    assert await app.count(app.tables.provider_transactions) == 2
    assert await app.count(app.tables.settlements) == 1
    [case] = await app.rows(app.tables.review_cases)
    assert case.reason == reason.value
    webhook_fact = next(f for f in await facts(app) if f.first_source == FirstSource.WEBHOOK.value)
    assert webhook_fact.api_tx_id is None


@pytest.mark.parametrize("genuine_first", [True, False])
async def test_webhook_and_two_api_rows_sharing_its_reference_lose_nothing(
    app: App, genuine_first: bool
) -> None:
    """API row A2 is the webhook's money; B2 is another transfer with the same bank reference
    and amount but another code. Whatever the read order, A2 joins the webhook fact and B2
    gets a fact of its own."""
    reader, module = await setup(app, ReconcileMode.DETECT_ONLY)
    created = await app.intent(expires_at=app.clock.now() + timedelta(hours=2))
    await app.pay(code=created.payment_reference)
    genuine = api_row(
        f"{API_ID[:-2]}0a", account=app.m1.account_number, code=created.payment_reference
    )
    other = api_row(f"{API_ID[:-2]}0b", account=app.m1.account_number, code="OTHER777")
    rows = (genuine, other) if genuine_first else (other, genuine)
    reader.script(account_ref(app.m1), page(rows[0]), page(rows[1]))

    await reconcile(module, app.m1)
    await reconcile(module, app.m1)
    app.clock.advance(seconds=GRACE.total_seconds())
    await reconcile(module, app.m1)

    by_source = {fact.first_source: fact for fact in await facts(app)}
    assert len(await facts(app)) == 2
    assert by_source[FirstSource.WEBHOOK.value].api_tx_id == genuine.source_tx_id
    assert by_source[FirstSource.RECONCILE.value].api_tx_id == other.source_tx_id
    api = {row.source_tx_id: row for row in await observations(app, ObservationSource.API)}
    assert api[genuine.source_tx_id].link_method == LinkMethod.BANK_REFERENCE.value
    assert api[other.source_tx_id].transaction_id == by_source[FirstSource.RECONCILE.value].id
    assert all(row.link_status == LinkStatus.LINKED.value for row in api.values())
    assert await app.count(app.tables.settlements) == 1


@pytest.mark.parametrize("mode", MODES)
async def test_ambiguous_sighting_gets_its_own_fact_and_is_never_settled(
    app: App, mode: ReconcileMode
) -> None:
    reader, module = await setup(app, mode)
    created = await app.intent(expires_at=app.clock.now() + timedelta(hours=2))
    # Two webhook transfers share the bank reference and amount but name no intent.
    await app.pay(content="chuyen tien")
    await app.pay(content="chuyen tien")
    # The API sighting names a real intent of the exact amount: still never auto-settled.
    reader.script(
        account_ref(app.m1),
        page(api_row(API_ID, account=app.m1.account_number, code=created.payment_reference)),
    )

    first = await reconcile(module, app.m1)
    app.clock.advance(seconds=GRACE.total_seconds())
    second = await reconcile(module, app.m1)

    assert first.counts["ambiguous"] == 1
    assert second.counts["created"] == 1
    [api] = await observations(app, ObservationSource.API)
    assert api.link_status == LinkStatus.LINKED.value
    api_fact = next(f for f in await facts(app) if f.first_source == FirstSource.RECONCILE.value)
    assert api.transaction_id == api_fact.id
    assert api_fact.match_state == MatchState.IN_REVIEW.value
    t = app.tables.review_cases
    [case] = await app.rows(t, t.c.transaction_id == api_fact.id)
    assert case.reason == ReviewReason.UNVERIFIED_IDENTITY.value
    assert case.details == {"source": "api", "ambiguous_bank_reference": "true"}
    assert await app.count(app.tables.settlements) == 0
    assert len(await facts(app)) == 3
