"""OperatorOverride: an operator can name the intent or waive lateness, never the amount."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from payment_module.domain.enums import (
    Direction,
    Environment,
    IntentStatus,
    ReceiptTimeSource,
    ReviewReason,
)
from payment_module.domain.errors import PolicyViolation
from payment_module.domain.matching.match_transaction import (
    MatchContext,
    MatchTransaction,
    OperatorOverride,
)
from payment_module.domain.money import AmountVnd
from payment_module.domain.review import MatchDecision, Review, Settle

OVERRIDES = [
    OperatorOverride(),
    OperatorOverride(accept_late=True),
    pytest.param("forced", id="forced"),
    pytest.param("forced-late", id="forced-late"),
]


@pytest.fixture
def ctx(make_tx, make_intent, connection, account_id, now):
    def build(tx=None, intent=None, *, memo=None, received_at=None) -> MatchContext:
        tx = tx or make_tx()
        intent = intent or make_intent()
        return MatchContext(
            tx=tx,
            connection=connection,
            bound_account_ids=frozenset({account_id}),
            tokens=(memo,) if memo else (intent.payment_reference,),
            candidates={intent.payment_reference: intent},
            effective_received_at=received_at or now,
            time_source=ReceiptTimeSource.WEBHOOK_RECEIVED,
        )

    return build


def _override(value, intent_id):
    if value == "forced":
        return OperatorOverride(forced_intent_id=intent_id)
    if value == "forced-late":
        return OperatorOverride(forced_intent_id=intent_id, accept_late=True)
    return value


def test_forced_intent_replaces_reference_resolution(ctx, make_intent) -> None:
    intent = make_intent()
    context = ctx(intent=intent, memo="NO-CODE-HERE")

    assert MatchTransaction().decide(context) == Review(ReviewReason.NO_REFERENCE)
    forced = MatchTransaction().decide(context, OperatorOverride(forced_intent_id=intent.id))
    assert forced == Settle(intent.id)


def test_forced_intent_must_be_loaded(ctx) -> None:
    with pytest.raises(PolicyViolation):
        MatchTransaction().decide(ctx(), OperatorOverride(forced_intent_id=uuid4()))


def test_accept_late_settles_exact_late_money(ctx, make_intent, now) -> None:
    intent = make_intent(status=IntentStatus.EXPIRED, expires_at=now - timedelta(minutes=5))
    context = ctx(intent=intent)

    assert MatchTransaction().decide(context).reason == ReviewReason.LATE
    accepted = MatchTransaction().decide(context, OperatorOverride(accept_late=True))
    assert accepted == Settle(intent.id)


@pytest.mark.parametrize("value", OVERRIDES)
@pytest.mark.parametrize("delta", [-1, 1, -50_000, 50_000])
@pytest.mark.parametrize("late", [False, True])
def test_no_override_settles_a_different_amount(
    ctx, make_tx, make_intent, now, value, delta, late
) -> None:
    intent = make_intent(amount=AmountVnd(100_000))
    tx = make_tx(amount=AmountVnd(100_000 + delta))
    received_at = intent.expires_at + timedelta(minutes=1) if late else now

    outcome = MatchTransaction().decide(
        ctx(tx, intent, received_at=received_at), _override(value, intent.id)
    )

    assert outcome == Review(ReviewReason.AMOUNT_MISMATCH, intent.id, outcome.details)


class LateForEverything:
    """A buggy host policy that reports every payment as late, whatever its amount."""

    def decide(self, tx, intent, is_late):
        return MatchDecision.review(ReviewReason.LATE)


def test_accept_late_does_not_trust_a_policy_that_hides_an_amount_mismatch(
    ctx, make_tx, make_intent
) -> None:
    intent = make_intent(amount=AmountVnd(100_000))
    tx = make_tx(amount=AmountVnd(99_000))

    outcome = MatchTransaction(LateForEverything()).decide(
        ctx(tx, intent), OperatorOverride(forced_intent_id=intent.id, accept_late=True)
    )

    assert outcome == Review(ReviewReason.AMOUNT_MISMATCH, intent.id)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"direction": Direction.OUT}, None),
        ({"receiving_account_id": uuid4()}, ReviewReason.RECEIVER_UNBOUND),
    ],
)
def test_override_keeps_the_guard(ctx, make_tx, make_intent, change, reason) -> None:
    intent = make_intent()
    outcome = MatchTransaction().decide(
        ctx(make_tx(**change), intent),
        OperatorOverride(forced_intent_id=intent.id, accept_late=True),
    )
    assert not isinstance(outcome, Settle)
    if reason is not None:
        assert outcome == Review(reason)


@pytest.mark.parametrize(
    "change",
    [
        {"tenant_id": "tenant-b"},
        {"environment": Environment.TEST},
        {"receiving_account_id": uuid4()},
    ],
)
def test_forced_intent_of_another_scope_is_a_tenant_mismatch(ctx, make_intent, change) -> None:
    intent = make_intent(**change)
    outcome = MatchTransaction().decide(
        ctx(intent=intent), OperatorOverride(forced_intent_id=intent.id)
    )
    assert isinstance(outcome, Review)
    assert outcome.reason == ReviewReason.TENANT_MISMATCH


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (IntentStatus.PAID, ReviewReason.ALREADY_PAID),
        (IntentStatus.CANCELLED, ReviewReason.INTENT_CANCELLED),
        (IntentStatus.SUPERSEDED, ReviewReason.INTENT_SUPERSEDED),
    ],
)
def test_forced_intent_keeps_eligibility(ctx, make_intent, status, reason) -> None:
    intent = make_intent(status=status)
    outcome = MatchTransaction().decide(
        ctx(intent=intent), OperatorOverride(forced_intent_id=intent.id, accept_late=True)
    )
    assert isinstance(outcome, Review)
    assert outcome.reason == reason
