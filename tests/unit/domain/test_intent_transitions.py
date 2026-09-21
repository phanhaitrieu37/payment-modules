from __future__ import annotations

import itertools
from datetime import datetime

import pytest

from payment_module.domain.enums import IntentStatus
from payment_module.domain.errors import IllegalTransition
from payment_module.domain.intent import transition_intent

S = IntentStatus
ALLOWED = {
    (S.AWAITING_PAYMENT, S.PAID),
    (S.AWAITING_PAYMENT, S.EXPIRED),
    (S.AWAITING_PAYMENT, S.CANCELLED),
    (S.AWAITING_PAYMENT, S.SUPERSEDED),
    (S.EXPIRED, S.PAID),
}
ALL_PAIRS = list(itertools.product(IntentStatus, IntentStatus))


@pytest.mark.parametrize(("current", "target"), sorted(ALLOWED))
def test_allowed_intent_transitions(current: IntentStatus, target: IntentStatus) -> None:
    assert transition_intent(current, target) is target


@pytest.mark.parametrize(("current", "target"), [pair for pair in ALL_PAIRS if pair not in ALLOWED])
def test_every_other_intent_transition_is_illegal(
    current: IntentStatus, target: IntentStatus
) -> None:
    with pytest.raises(IllegalTransition):
        transition_intent(current, target)


def test_intent_requires_timezone_aware_expiry(make_intent) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        make_intent(expires_at=datetime(2026, 9, 22, 10, 0))
