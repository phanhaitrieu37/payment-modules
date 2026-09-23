"""The replaceable matching step and its default, exact-amount implementation."""

from __future__ import annotations

from typing import Protocol

from payment_module.domain.enums import ReviewReason
from payment_module.domain.intent import IntentView
from payment_module.domain.review import MatchDecision
from payment_module.domain.transaction import TransactionView


class MatchingPolicy(Protocol):
    """The only step a host may replace. It can only tighten what the core allows.

    It may return ``SETTLE`` or ``REVIEW`` with ``AMOUNT_MISMATCH`` or ``LATE``. The core
    rejects any other answer and any ``SETTLE`` that is late or for a different amount.
    """

    def decide(self, tx: TransactionView, intent: IntentView, is_late: bool) -> MatchDecision: ...


class ExactAmountPolicy:
    """Settle only an on-time payment of exactly the intent amount.

    Amount is checked before lateness: a wrong amount can never be fixed by accepting late
    money, so it must surface as ``AMOUNT_MISMATCH``.
    """

    def decide(self, tx: TransactionView, intent: IntentView, is_late: bool) -> MatchDecision:
        if tx.amount != intent.amount:
            direction = "under" if tx.amount < intent.amount else "over"
            return MatchDecision.review(ReviewReason.AMOUNT_MISMATCH, {"direction": direction})
        if is_late:
            return MatchDecision.review(ReviewReason.LATE)
        return MatchDecision.settle()
