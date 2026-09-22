"""ResolveReview: five resolutions, none of which can settle money of another amount."""

from __future__ import annotations

import contextlib
import uuid

import pytest
import sqlalchemy as sa

from fakes.payment_app import App, fact_of, insert_intent, mismatched_settlements, open_case
from payment_module.domain.enums import (
    IntentStatus,
    MatchState,
    ReviewCaseStatus,
    ReviewReason,
    ReviewResolution,
    SettlementOrigin,
)
from payment_module.domain.errors import (
    OnboardingRejected,
    ResolutionNotAllowed,
    ReviewAlreadyResolved,
    ReviewCaseNotFound,
)

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

ACTOR = "ops@example.test"


async def mismatch_case(app: App, *, intent_amount: int = 100_000, paid: int = 99_000):
    """An intent and a payment of another amount for its reference: an AMOUNT_MISMATCH case."""
    created = await app.intent(amount_vnd=intent_amount)
    await app.pay(code=created.payment_reference, amount=paid)
    case = await open_case(app)
    assert case.reason == ReviewReason.AMOUNT_MISMATCH.value
    assert case.candidate_intent_id == created.intent.id
    return created, case


async def resolve(app: App, case, resolution: ReviewResolution, **kwargs):
    kwargs.setdefault("note", "checked with the bank statement")
    return await app.module.resolve_review.execute(
        app.m1.tenant_id, case.id, resolution, ACTOR, **kwargs
    )


async def settlements(app: App) -> list[sa.Row]:
    return await app.rows(app.tables.settlements)


async def outbox(app: App, event_type: str) -> list[sa.Row]:
    t = app.tables.outbox_events
    return await app.rows(t, t.c.event_type == event_type)


async def test_amount_mismatch_cannot_attach_to_its_own_intent(app: App) -> None:
    created, case = await mismatch_case(app)

    with pytest.raises(ResolutionNotAllowed) as caught:
        await resolve(app, case, ReviewResolution.ATTACH_TO_INTENT, intent_id=created.intent.id)
    assert caught.value.code == ReviewReason.AMOUNT_MISMATCH.value
    with pytest.raises(ResolutionNotAllowed):
        await resolve(app, case, ReviewResolution.ACCEPT_LATE)

    assert await settlements(app) == []
    assert (await open_case(app)).id == case.id


async def test_amount_mismatch_attaches_to_another_intent_of_that_amount(app: App) -> None:
    _, case = await mismatch_case(app)
    other = await app.intent(amount_vnd=99_000)

    view = await resolve(
        app,
        case,
        ReviewResolution.ATTACH_TO_INTENT,
        intent_id=other.intent.id,
        resolution_ref="matched-by-phone",
    )

    assert view.status == ReviewCaseStatus.RESOLVED
    assert view.match_state == MatchState.SETTLED
    [settlement] = await settlements(app)
    assert settlement.id == view.settlement_id
    assert settlement.intent_id == other.intent.id
    assert settlement.amount_vnd == settlement.intent_amount_vnd == 99_000
    assert settlement.origin == SettlementOrigin.OPERATOR_REVIEW.value
    assert (settlement.review_case_id, settlement.resolved_by) == (case.id, ACTOR)
    t = app.tables.review_cases
    [row] = await app.rows(t, t.c.id == case.id)
    assert (row.status, row.resolution, row.resolved_by, row.resolution_ref) == (
        "resolved",
        "attach_to_intent",
        ACTOR,
        "matched-by-phone",
    )
    assert row.resolved_at is not None
    [event] = await outbox(app, "ReviewResolved")
    assert event.payload["settlement_id"] == str(settlement.id)
    assert event.payload["resolution"] == "attach_to_intent"
    outcome = app.observer.outcomes[-1]
    assert (outcome.match_state, outcome.settlement_id) == (MatchState.SETTLED, settlement.id)
    assert app.handler.calls[-1].origin == SettlementOrigin.OPERATOR_REVIEW


async def test_mark_external_closes_the_fact_and_tells_the_observer(app: App) -> None:
    _, case = await mismatch_case(app)

    with pytest.raises(ResolutionNotAllowed) as caught:
        await resolve(app, case, ReviewResolution.MARK_EXTERNAL, note=None)
    assert caught.value.code == "PROVENANCE_REQUIRED"
    view = await resolve(app, case, ReviewResolution.MARK_EXTERNAL, resolution_ref="refunded")

    assert view.match_state == MatchState.CLOSED_EXTERNAL
    assert (await fact_of(app, case.transaction_id)).match_state == "closed_external"
    outcome = app.observer.outcomes[-1]
    assert (outcome.match_state, outcome.review_case_id) == (MatchState.CLOSED_EXTERNAL, case.id)
    [event] = await outbox(app, "ReviewResolved")
    assert event.payload["settlement_id"] is None
    assert await settlements(app) == []


@pytest.mark.parametrize("resolution", list(ReviewResolution))
@pytest.mark.parametrize("paid", [99_999, 100_001, 50_000, 200_000])
@pytest.mark.parametrize("reason", [ReviewReason.AMOUNT_MISMATCH, ReviewReason.LATE])
async def test_no_resolution_settles_mismatched_amount(
    app: App, resolution: ReviewResolution, paid: int, reason: ReviewReason
) -> None:
    """Every resolution against the wrong-amount intent, including a case forged to read
    ``LATE``, leaves no settlement whose amounts differ."""
    created, case = await mismatch_case(app, intent_amount=100_000, paid=paid)
    if reason == ReviewReason.LATE:
        t = app.tables.review_cases
        await app.execute(sa.update(t).where(t.c.id == case.id).values(reason=reason.value))
    earlier = await app.pay(amount=paid)  # a fact of the same amount, for mark_duplicate_of
    kwargs = {
        "intent_id": created.intent.id,
        "duplicate_of_transaction_id": (await fact_by_inbox(app, earlier.inbox_id)).id,
        "receiving_account_id": app.m1.account_id,
    }

    with contextlib.suppress(ResolutionNotAllowed, OnboardingRejected):
        await resolve(app, case, resolution, **kwargs)

    assert await mismatched_settlements(app) == 0
    s = app.tables.settlements
    assert await app.count(s, s.c.transaction_id == case.transaction_id) == 0
    intents = app.tables.payment_intents
    [intent] = await app.rows(intents, intents.c.id == created.intent.id)
    assert intent.status == IntentStatus.AWAITING_PAYMENT.value


async def fact_by_inbox(app: App, inbox_id: uuid.UUID) -> sa.Row:
    o = app.tables.provider_observations
    [observation] = await app.rows(o, o.c.inbox_id == inbox_id)
    return await fact_of(app, observation.transaction_id)


async def test_accept_late_settles_exact_late_money_with_audit(app: App) -> None:
    created = await app.intent(amount_vnd=120_000)
    app.clock.advance(minutes=20)
    await app.pay(code=created.payment_reference, amount=120_000)
    case = await open_case(app)
    assert case.reason == ReviewReason.LATE.value

    with pytest.raises(ResolutionNotAllowed):
        await resolve(app, case, ReviewResolution.ATTACH_TO_INTENT, intent_id=created.intent.id)
    view = await resolve(app, case, ReviewResolution.ACCEPT_LATE)

    assert view.match_state == MatchState.SETTLED
    [settlement] = await settlements(app)
    assert settlement.origin == SettlementOrigin.OPERATOR_REVIEW.value
    assert (settlement.review_case_id, settlement.resolved_by) == (case.id, ACTOR)
    intents = app.tables.payment_intents
    [intent] = await app.rows(intents, intents.c.id == created.intent.id)
    assert intent.status == IntentStatus.PAID.value


async def test_accept_late_is_only_for_late_cases(app: App) -> None:
    await app.pay(code="NOTHING-HERE")
    case = await open_case(app)
    assert case.reason == ReviewReason.NO_REFERENCE.value

    with pytest.raises(ResolutionNotAllowed) as caught:
        await resolve(app, case, ReviewResolution.ACCEPT_LATE)
    assert caught.value.code == "RESOLUTION_NOT_ALLOWED"


async def test_attach_to_an_intent_of_another_receiver_is_refused(app: App) -> None:
    await app.pay(code="NOTHING-HERE", amount=150_000)
    case = await open_case(app)
    other_merchant = await app.intent(app.m2, amount_vnd=150_000)

    with pytest.raises(ResolutionNotAllowed) as caught:
        await resolve(
            app, case, ReviewResolution.ATTACH_TO_INTENT, intent_id=other_merchant.intent.id
        )
    assert caught.value.code == ReviewReason.TENANT_MISMATCH.value
    assert await settlements(app) == []


async def test_tenant_mismatch_attaches_only_within_the_tenant(app: App) -> None:
    foreign = await app.intent(app.mb, amount_vnd=150_000)
    await app.pay(code=foreign.payment_reference, amount=150_000)
    case = await open_case(app)
    assert case.reason == ReviewReason.TENANT_MISMATCH.value
    own = await app.intent(amount_vnd=150_000)

    with pytest.raises(ResolutionNotAllowed) as caught:
        await resolve(app, case, ReviewResolution.ATTACH_TO_INTENT, intent_id=foreign.intent.id)
    assert caught.value.code == "INTENT_NOT_FOUND"
    view = await resolve(app, case, ReviewResolution.ATTACH_TO_INTENT, intent_id=own.intent.id)

    assert view.match_state == MatchState.SETTLED
    [settlement] = await settlements(app)
    assert (settlement.tenant_id, settlement.intent_id) == (app.m1.tenant_id, own.intent.id)


async def duplicate_pair(app: App, *, original_ref: str, second_ref: str):
    """A settled payment and a second payment of the same money in review."""
    created = await app.intent(amount_vnd=150_000)
    first = await app.pay(code=created.payment_reference, bank_reference=original_ref)
    original = await fact_by_inbox(app, first.inbox_id)
    await app.pay(code="NOTHING-HERE", bank_reference=second_ref)
    return original, await open_case(app)


async def test_mark_duplicate_of_needs_the_same_money(app: App) -> None:
    created = await app.intent(amount_vnd=150_000)
    first = await app.pay(code=created.payment_reference, amount=150_000)
    original = await fact_by_inbox(app, first.inbox_id)
    other_amount = await fact_by_inbox(app, (await app.pay(amount=70_000)).inbox_id)
    await app.pay(code="NOTHING-HERE", amount=150_000)
    t = app.tables.review_cases
    [case] = await app.rows(t, t.c.status == "open", t.c.transaction_id.notin_([other_amount.id]))

    for target, code in [
        (None, "DUPLICATE_TARGET_REQUIRED"),
        (other_amount.id, "DUPLICATE_TARGET_MISMATCH"),
        (case.transaction_id, "DUPLICATE_TARGET_MISMATCH"),
        (uuid.uuid4(), "DUPLICATE_TARGET_NOT_FOUND"),
    ]:
        with pytest.raises(ResolutionNotAllowed) as caught:
            await resolve(
                app,
                case,
                ReviewResolution.MARK_DUPLICATE_OF,
                duplicate_of_transaction_id=target,
                resolution_ref="bank-statement-2026-09",
            )
        assert caught.value.code == code
    assert (await fact_of(app, original.id)).match_state == "settled"


async def test_duplicate_without_shared_bank_reference_or_ref_needs_evidence(app: App) -> None:
    """Same account, amount and direction plus a free-text note is not identity proof."""
    original, case = await duplicate_pair(app, original_ref="FT-A", second_ref="FT-B")

    for kwargs in ({"note": "checked"}, {"note": None}):
        with pytest.raises(ResolutionNotAllowed) as caught:
            await resolve(
                app,
                case,
                ReviewResolution.MARK_DUPLICATE_OF,
                duplicate_of_transaction_id=original.id,
                **kwargs,
            )
        assert caught.value.code == "DUPLICATE_EVIDENCE_REQUIRED"
    assert (await open_case(app)).id == case.id
    assert (await fact_of(app, case.transaction_id)).match_state == "in_review"


async def test_duplicate_with_a_shared_bank_reference_is_accepted(app: App) -> None:
    original, case = await duplicate_pair(app, original_ref="FT-SAME", second_ref="FT-SAME")

    view = await resolve(
        app,
        case,
        ReviewResolution.MARK_DUPLICATE_OF,
        duplicate_of_transaction_id=original.id,
        note=None,
    )

    assert view.match_state == MatchState.DUPLICATE_OF
    row = await fact_of(app, case.transaction_id)
    assert (row.match_state, row.duplicate_of_transaction_id) == ("duplicate_of", original.id)
    t = app.tables.review_cases
    [resolved] = await app.rows(t, t.c.id == case.id)
    assert resolved.details["duplicate_of"] == str(original.id)
    assert resolved.details["duplicate_evidence"] == "bank_reference"
    assert app.observer.outcomes[-1].match_state == MatchState.DUPLICATE_OF


async def test_duplicate_with_a_resolution_ref_is_accepted(app: App) -> None:
    original, case = await duplicate_pair(app, original_ref="FT-A", second_ref="FT-B")

    view = await resolve(
        app,
        case,
        ReviewResolution.MARK_DUPLICATE_OF,
        duplicate_of_transaction_id=original.id,
        resolution_ref="bank-dispute-4711",
    )

    assert view.match_state == MatchState.DUPLICATE_OF
    t = app.tables.review_cases
    [resolved] = await app.rows(t, t.c.id == case.id)
    assert (resolved.resolution_ref, resolved.details["duplicate_evidence"]) == (
        "bank-dispute-4711",
        "resolution_ref",
    )
    assert resolved.resolution_note == "checked with the bank statement"


async def test_system_resolution_is_never_an_operator_choice(app: App) -> None:
    _, case = await mismatch_case(app)
    with pytest.raises(ResolutionNotAllowed) as caught:
        await resolve(app, case, ReviewResolution.SETTLED_BY_WEBHOOK)
    assert caught.value.code == "RESOLUTION_NOT_ALLOWED"
    assert (await open_case(app)).id == case.id


async def test_a_resolved_case_cannot_be_resolved_again(app: App) -> None:
    _, case = await mismatch_case(app)
    await resolve(app, case, ReviewResolution.MARK_EXTERNAL)

    with pytest.raises(ReviewAlreadyResolved):
        await resolve(app, case, ReviewResolution.MARK_EXTERNAL)
    with pytest.raises(ReviewCaseNotFound):
        await app.module.resolve_review.execute(
            app.mb.tenant_id, case.id, ReviewResolution.MARK_EXTERNAL, ACTOR, "note"
        )


@pytest.mark.parametrize("ref", ["x" * 65, "has space", ""])
async def test_resolution_ref_is_a_short_structured_code(app: App, ref: str) -> None:
    _, case = await mismatch_case(app)
    with pytest.raises(ResolutionNotAllowed) as caught:
        await resolve(app, case, ReviewResolution.MARK_EXTERNAL, resolution_ref=ref)
    assert caught.value.code == "INVALID_RESOLUTION_REF"


async def test_actor_is_required(app: App) -> None:
    _, case = await mismatch_case(app)
    with pytest.raises(OnboardingRejected):
        await app.module.resolve_review.execute(
            app.m1.tenant_id, case.id, ReviewResolution.MARK_EXTERNAL, " ", "note"
        )


async def test_bind_receiver_binds_and_settles(app: App) -> None:
    intent = await insert_intent(app, app.m1_unbound_account, amount=80_000)
    await app.pay(code=intent.payment_reference, amount=80_000, account=app.m1_unbound_account)
    case = await open_case(app)
    assert case.reason == ReviewReason.RECEIVER_UNBOUND.value

    view = await resolve(
        app,
        case,
        ReviewResolution.BIND_RECEIVER,
        receiving_account_id=intent.receiving_account_id,
        resolution_ref="account-onboarded",
    )

    assert (view.status, view.resolution) == (
        ReviewCaseStatus.RESOLVED,
        ReviewResolution.BIND_RECEIVER,
    )
    assert view.resolution_ref == "account-onboarded"
    assert view.match_state == MatchState.SETTLED
    [settlement] = await settlements(app)
    assert settlement.intent_id == intent.id
    b = app.tables.connection_account_bindings
    assert await app.count(b, b.c.receiving_account_id == intent.receiving_account_id) == 1
    c = app.tables.provider_connections
    [connection] = await app.rows(c, c.c.id == app.m1.connection_id)
    assert (connection.status, connection.status_changed_by) == ("not_ready", ACTOR)
    [event] = await outbox(app, "ReviewResolved")
    assert event.payload["settlement_id"] == str(settlement.id)


async def test_bind_receiver_refuses_an_account_of_another_merchant(app: App) -> None:
    await app.pay(code="NOTHING-HERE", account=app.m2.account_number)
    case = await open_case(app)
    assert case.reason == ReviewReason.RECEIVER_UNBOUND.value

    with pytest.raises(OnboardingRejected) as caught:
        await resolve(
            app, case, ReviewResolution.BIND_RECEIVER, receiving_account_id=app.m2.account_id
        )
    assert caught.value.code == "SCOPE_MISMATCH"
    with pytest.raises(ResolutionNotAllowed) as caught:
        await resolve(
            app, case, ReviewResolution.BIND_RECEIVER, receiving_account_id=app.m1.account_id
        )
    assert caught.value.code == "ACCOUNT_KEY_MISMATCH"
    assert (await open_case(app)).id == case.id


async def test_bind_receiver_is_only_for_unbound_money(app: App) -> None:
    _, case = await mismatch_case(app)
    with pytest.raises(ResolutionNotAllowed) as caught:
        await resolve(
            app, case, ReviewResolution.BIND_RECEIVER, receiving_account_id=app.m1.account_id
        )
    assert caught.value.code == "RESOLUTION_NOT_ALLOWED"
