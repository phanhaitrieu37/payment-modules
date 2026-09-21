"""Payment intent snapshot and lifecycle.

An intent reaches ``paid`` only through a committed settlement. Changing the amount creates
a new intent and supersedes the old one; accepting late money is an audited operator step.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from payment_module.domain.enums import Environment, IntentStatus
from payment_module.domain.errors import IllegalTransition
from payment_module.domain.money import AmountVnd

_INTENT_TRANSITIONS: dict[IntentStatus, frozenset[IntentStatus]] = {
    IntentStatus.AWAITING_PAYMENT: frozenset(
        {
            IntentStatus.PAID,
            IntentStatus.EXPIRED,
            IntentStatus.CANCELLED,
            IntentStatus.SUPERSEDED,
        }
    ),
    IntentStatus.EXPIRED: frozenset({IntentStatus.PAID}),
    IntentStatus.PAID: frozenset(),
    IntentStatus.CANCELLED: frozenset(),
    IntentStatus.SUPERSEDED: frozenset(),
}


def ensure_aware(value: datetime, field_name: str) -> datetime:
    """Reject naive datetimes; the domain only compares timezone-aware instants."""
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


@dataclass(frozen=True, slots=True)
class IntentView:
    """The committed intent fields the matching chain needs."""

    id: UUID
    tenant_id: str
    merchant_id: UUID
    environment: Environment
    receiving_account_id: UUID
    amount: AmountVnd
    status: IntentStatus
    payment_reference: str
    expires_at: datetime
    superseded_by_intent_id: UUID | None = None

    def __post_init__(self) -> None:
        ensure_aware(self.expires_at, "expires_at")


def transition_intent(current: IntentStatus, target: IntentStatus) -> IntentStatus:
    """Return ``target`` when the intent lifecycle allows ``current -> target``."""
    if target not in _INTENT_TRANSITIONS[current]:
        raise IllegalTransition("payment_intent", current.value, target.value)
    return target
