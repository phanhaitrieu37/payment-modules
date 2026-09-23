"""Policy decisions and matching outcomes.

``MatchDecision`` is what a :class:`MatchingPolicy` may return. ``MatchOutcome`` is what the
core chain returns after its own checks: settle one intent, open a review, or record that the
fact is not a payment to match (outgoing or unknown direction).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from uuid import UUID

from payment_module.domain.enums import DecisionOutcome, ReviewReason, coerce_enum_fields


def _frozen_details(details: Mapping[str, str]) -> Mapping[str, str]:
    return MappingProxyType(dict(details))


@dataclass(frozen=True, slots=True)
class MatchDecision:
    outcome: DecisionOutcome
    reason: ReviewReason | None = None
    details: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        coerce_enum_fields(self, outcome=DecisionOutcome, reason=ReviewReason)
        if (self.outcome == DecisionOutcome.REVIEW) != (self.reason is not None):
            raise ValueError("a REVIEW decision needs a reason and a SETTLE decision has none")
        object.__setattr__(self, "details", _frozen_details(self.details))

    @classmethod
    def settle(cls) -> MatchDecision:
        return cls(DecisionOutcome.SETTLE)

    @classmethod
    def review(
        cls, reason: ReviewReason, details: Mapping[str, str] | None = None
    ) -> MatchDecision:
        return cls(DecisionOutcome.REVIEW, reason, details or {})


@dataclass(frozen=True, slots=True)
class Settle:
    intent_id: UUID


@dataclass(frozen=True, slots=True)
class Review:
    reason: ReviewReason
    candidate_intent_id: UUID | None = None
    details: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        coerce_enum_fields(self, reason=ReviewReason)
        object.__setattr__(self, "details", _frozen_details(self.details))


@dataclass(frozen=True, slots=True)
class NotApplicable:
    """Outgoing or unknown-direction money: recorded as a fact, never reviewed."""


type MatchOutcome = Settle | Review | NotApplicable
