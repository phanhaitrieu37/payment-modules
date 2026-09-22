"""RematchUnbound and RematchReviews: matching again after the operator fixed the cause."""

from __future__ import annotations

from datetime import timedelta

import pytest

from fakes.payment_app import App, fact_of, insert_intent, open_case
from payment_module.domain.enums import MatchState, ReviewReason
from payment_module.domain.errors import OnboardingRejected

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

ACTOR = "ops@example.test"
NEW_ACCOUNT = "7770001"


async def cases(app: App):
    t = app.tables.review_cases
    return await app.rows(t)


async def register(app: App, number: str = NEW_ACCOUNT):
    return await app.module.register_receiving_account.execute(
        app.m1.tenant_id, app.m1.merchant_id, app.m1.environment, "VCB", number, "SHOP", ACTOR
    )


async def test_unbound_money_settles_after_bind_and_rematch(app: App) -> None:
    """Money into an account nobody registered yet: the fact has no receiver. Once the
    account is registered and bound, the rematch gives it the receiver and settles it."""
    await app.pay(code="SUBLATEREG1", amount=90_000, account=NEW_ACCOUNT)
    [case] = await cases(app)
    fact = await fact_of(app, case.transaction_id)
    assert (case.reason, fact.receiving_account_id) == (ReviewReason.RECEIVER_UNBOUND.value, None)
    account = await register(app)
    intent = await insert_intent(app, NEW_ACCOUNT, amount=90_000, payment_reference="SUBLATEREG1")

    nothing = await app.module.rematch_unbound.execute(
        app.m1.tenant_id, app.m1.connection_id, ACTOR
    )
    assert [result.changed for result in nothing] == [False]
    await app.module.bind_connection_account.execute(
        app.m1.tenant_id, app.m1.connection_id, account.id, ACTOR
    )
    [result] = await app.module.rematch_unbound.execute(
        app.m1.tenant_id, app.m1.connection_id, ACTOR
    )

    assert (result.changed, result.match_state) == (True, MatchState.SETTLED)
    fact = await fact_of(app, case.transaction_id)
    assert (fact.receiving_account_id, fact.merchant_id) == (account.id, app.m1.merchant_id)
    assert fact.match_state == MatchState.SETTLED.value
    [settlement] = await app.rows(app.tables.settlements)
    assert settlement.intent_id == intent.id
    [resolved] = await cases(app)
    assert (resolved.status, resolved.resolution, resolved.resolved_by) == (
        "resolved",
        "bind_receiver",
        ACTOR,
    )
    t = app.tables.outbox_events
    [event] = await app.rows(t, t.c.event_type == "ReviewResolved")
    assert event.payload["settlement_id"] == str(settlement.id)


async def test_rematch_after_bind_can_open_a_case_of_another_reason(app: App) -> None:
    await app.pay(code="SUBWRONGAMT", amount=90_001, account=NEW_ACCOUNT)
    account = await register(app)
    await insert_intent(app, NEW_ACCOUNT, amount=90_000, payment_reference="SUBWRONGAMT")
    await app.module.bind_connection_account.execute(
        app.m1.tenant_id, app.m1.connection_id, account.id, ACTOR
    )

    [result] = await app.module.rematch_unbound.execute(
        app.m1.tenant_id, app.m1.connection_id, ACTOR
    )

    assert (result.changed, result.review_reason) == (True, ReviewReason.AMOUNT_MISMATCH)
    rows = {row.reason: row for row in await cases(app)}
    assert rows[ReviewReason.RECEIVER_UNBOUND.value].status == "resolved"
    assert rows[ReviewReason.AMOUNT_MISMATCH.value].status == "open"
    assert await app.count(app.tables.settlements) == 0


async def test_rematch_unbound_only_touches_facts_of_that_connection(app: App) -> None:
    await app.pay(app.m2, code="NOTHING-HERE", account="8880001")

    results = await app.module.rematch_unbound.execute(
        app.m1.tenant_id, app.m1.connection_id, ACTOR
    )

    assert results == []
    assert (await open_case(app)).reason == ReviewReason.RECEIVER_UNBOUND.value


async def test_receiver_spoof_binding_to_another_merchant_is_refused(app: App) -> None:
    """Money reported for m2's account through m1's connection stays in review: m1's
    connection cannot be bound to an account m1 does not own."""
    await app.pay(code="NOTHING-HERE", account=app.m2.account_number)

    with pytest.raises(OnboardingRejected) as caught:
        await app.module.bind_connection_account.execute(
            app.m1.tenant_id, app.m1.connection_id, app.m2.account_id, ACTOR
        )
    assert caught.value.code == "SCOPE_MISMATCH"
    [result] = await app.module.rematch_unbound.execute(
        app.m1.tenant_id, app.m1.connection_id, ACTOR
    )
    assert result.changed is False
    assert (await open_case(app)).reason == ReviewReason.RECEIVER_UNBOUND.value


async def legacy_intent(app: App, code: str, amount: int = 150_000):
    await app.module.import_legacy_reference_profile.execute(7, ACTOR)
    return await app.intent(
        prefix_name=None,
        reference_override=code,
        reference_profile_version=7,
        amount_vnd=amount,
    )


async def test_rematch_reviews_settles_after_legacy_import(app: App) -> None:
    await app.pay(code=None, content="don hang OLDSHOP-42", amount=150_000)
    case = await open_case(app)
    assert case.reason == ReviewReason.NO_REFERENCE.value
    created = await legacy_intent(app, "OLDSHOP-42")

    [result] = await app.module.rematch_reviews.execute(app.m1.tenant_id, ACTOR)

    assert (result.changed, result.match_state) == (True, MatchState.SETTLED)
    [settlement] = await app.rows(app.tables.settlements)
    assert settlement.intent_id == created.intent.id
    [resolved] = await cases(app)
    assert (resolved.status, resolved.resolution) == ("resolved", "attach_to_intent")


async def test_rematch_reviews_updates_the_reason_in_place(app: App) -> None:
    await app.pay(code=None, content="OLDSHOP-43", amount=150_001)
    case = await open_case(app)
    await legacy_intent(app, "OLDSHOP-43", amount=150_000)

    [result] = await app.module.rematch_reviews.execute(app.m1.tenant_id, ACTOR)

    assert (result.changed, result.review_reason) == (True, ReviewReason.AMOUNT_MISMATCH)
    [row] = await cases(app)
    assert (row.id, row.status, row.reason) == (case.id, "open", "AMOUNT_MISMATCH")


async def test_rematch_reviews_leaves_cases_that_still_have_no_reference(app: App) -> None:
    await app.pay(code=None, content="nothing to see", amount=150_000)

    [result] = await app.module.rematch_reviews.execute(app.m1.tenant_id, ACTOR)

    assert result.changed is False
    t = app.tables.outbox_events
    assert [row.event_type for row in await app.rows(t)] == ["PaymentNeedsReview"]


async def test_rematch_reviews_honours_opened_after(app: App) -> None:
    await app.pay(code=None, content="OLDSHOP-44", amount=150_000)
    await legacy_intent(app, "OLDSHOP-44")

    results = await app.module.rematch_reviews.execute(
        app.m1.tenant_id, ACTOR, opened_after=app.clock.now() + timedelta(minutes=1)
    )

    assert results == []
    assert (await open_case(app)).reason == ReviewReason.NO_REFERENCE.value


async def test_rematch_reviews_does_not_take_unbound_cases(app: App) -> None:
    with pytest.raises(ValueError):
        await app.module.rematch_reviews.execute(
            app.m1.tenant_id, ACTOR, reasons={ReviewReason.RECEIVER_UNBOUND}
        )
