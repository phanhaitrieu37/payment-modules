"""Third core step: the policy never sees a paid, cancelled or superseded intent."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from payment_module.domain.enums import IntentStatus, ReviewReason
from payment_module.domain.intent import IntentView, ensure_aware


@dataclass(frozen=True, slots=True)
class Eligible:
    is_late: bool


@dataclass(frozen=True, slots=True)
class Ineligible:
    reason: ReviewReason
    candidate_intent_id: UUID | None


type Eligibility = Eligible | Ineligible


class IntentEligibility:
    def check(self, intent: IntentView, effective_received_at: datetime) -> Eligibility:
        """Stop closed intents; mark money received after expiry as late.

        An ``expired`` intent, or an ``awaiting_payment`` one whose ``expires_at`` is before
        the receipt time, continues with ``is_late=True``.
        """
        ensure_aware(effective_received_at, "effective_received_at")
        match intent.status:
            case IntentStatus.PAID:
                return Ineligible(ReviewReason.ALREADY_PAID, intent.id)
            case IntentStatus.CANCELLED:
                return Ineligible(ReviewReason.INTENT_CANCELLED, intent.id)
            case IntentStatus.SUPERSEDED:
                return Ineligible(ReviewReason.INTENT_SUPERSEDED, intent.superseded_by_intent_id)
            case IntentStatus.EXPIRED:
                return Eligible(is_late=True)
            case IntentStatus.AWAITING_PAYMENT:
                return Eligible(is_late=effective_received_at > intent.expires_at)
