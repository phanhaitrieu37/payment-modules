from __future__ import annotations

import pytest

from payment_module.domain.enums import DecisionOutcome, ReviewReason
from payment_module.domain.matching.exact_amount_policy import ExactAmountPolicy
from payment_module.domain.money import AmountVnd
from payment_module.domain.review import MatchDecision


def test_exact_amount_on_time_settles(make_tx, make_intent) -> None:
    decision = ExactAmountPolicy().decide(make_tx(), make_intent(), is_late=False)
    assert decision == MatchDecision.settle()


@pytest.mark.parametrize(("amount", "direction"), [(149_999, "under"), (150_001, "over")])
@pytest.mark.parametrize("is_late", [False, True])
def test_amount_mismatch_wins_over_late(
    make_tx, make_intent, amount: int, direction: str, is_late: bool
) -> None:
    decision = ExactAmountPolicy().decide(make_tx(amount=AmountVnd(amount)), make_intent(), is_late)
    assert decision.outcome is DecisionOutcome.REVIEW
    assert decision.reason is ReviewReason.AMOUNT_MISMATCH
    assert dict(decision.details) == {"direction": direction}


def test_exact_amount_late_goes_to_review(make_tx, make_intent) -> None:
    decision = ExactAmountPolicy().decide(make_tx(), make_intent(), is_late=True)
    assert decision == MatchDecision.review(ReviewReason.LATE)


def test_decision_requires_reason_only_for_review() -> None:
    with pytest.raises(ValueError):
        MatchDecision(DecisionOutcome.REVIEW)
    with pytest.raises(ValueError):
        MatchDecision(DecisionOutcome.SETTLE, ReviewReason.LATE)
