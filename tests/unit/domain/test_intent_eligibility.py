from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from payment_module.domain.enums import IntentStatus, ReviewReason
from payment_module.domain.matching.intent_eligibility import (
    Eligible,
    Ineligible,
    IntentEligibility,
)

NOW = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (IntentStatus.PAID, ReviewReason.ALREADY_PAID),
        (IntentStatus.CANCELLED, ReviewReason.INTENT_CANCELLED),
    ],
)
def test_closed_intents_never_reach_policy(make_intent, status, reason) -> None:
    intent = make_intent(status=status)
    assert IntentEligibility().check(intent, NOW) == Ineligible(reason, intent.id)


def test_superseded_points_at_replacement(make_intent) -> None:
    replacement = uuid4()
    intent = make_intent(status=IntentStatus.SUPERSEDED, superseded_by_intent_id=replacement)
    assert IntentEligibility().check(intent, NOW) == Ineligible(
        ReviewReason.INTENT_SUPERSEDED, replacement
    )


def test_open_intent_on_time(make_intent) -> None:
    intent = make_intent()
    assert IntentEligibility().check(intent, intent.expires_at) == Eligible(is_late=False)


def test_open_intent_received_after_expiry_is_late(make_intent) -> None:
    intent = make_intent()
    received = intent.expires_at + timedelta(seconds=1)
    assert IntentEligibility().check(intent, received) == Eligible(is_late=True)


def test_expired_intent_received_after_expiry_is_late(make_intent) -> None:
    intent = make_intent(status=IntentStatus.EXPIRED)
    received = intent.expires_at + timedelta(seconds=1)
    assert IntentEligibility().check(intent, received) == Eligible(is_late=True)


@pytest.mark.parametrize("offset", [timedelta(0), timedelta(seconds=-1)])
def test_expired_intent_received_before_expiry_is_on_time(make_intent, offset) -> None:
    # The expiry job may flip the status before a timely payment is processed.
    intent = make_intent(status=IntentStatus.EXPIRED)
    received = intent.expires_at + offset
    assert IntentEligibility().check(intent, received) == Eligible(is_late=False)


def test_naive_receipt_time_rejected(make_intent) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        IntentEligibility().check(make_intent(), datetime(2026, 9, 22, 10, 0))
