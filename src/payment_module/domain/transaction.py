"""Canonical provider transaction (one row per movement of money) and its match state.

The bank fact is immutable; only ``match_state`` moves. Every fact ends in a terminal state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from payment_module.domain.enums import Direction, Environment, MatchState, coerce_enum_fields
from payment_module.domain.errors import IllegalTransition
from payment_module.domain.money import AmountVnd

_MATCH_TRANSITIONS: dict[MatchState, frozenset[MatchState]] = {
    MatchState.RECORDED: frozenset(
        {MatchState.SETTLED, MatchState.IN_REVIEW, MatchState.NOT_APPLICABLE}
    ),
    MatchState.IN_REVIEW: frozenset(
        {MatchState.SETTLED, MatchState.CLOSED_EXTERNAL, MatchState.DUPLICATE_OF}
    ),
    MatchState.SETTLED: frozenset(),
    MatchState.NOT_APPLICABLE: frozenset(),
    MatchState.CLOSED_EXTERNAL: frozenset(),
    MatchState.DUPLICATE_OF: frozenset(),
}


@dataclass(frozen=True, slots=True)
class TransactionView:
    """The canonical fact fields the matching chain needs.

    ``receiving_account_id`` and ``merchant_id`` are ``None`` while the reported account is
    not bound to any receiving account. Memo text and holder names are deliberately absent.
    """

    id: UUID
    tenant_id: str
    environment: Environment
    provider_account_key: str
    receiving_account_id: UUID | None
    merchant_id: UUID | None
    amount: AmountVnd
    direction: Direction
    match_state: MatchState
    bank_reference: str | None = None
    webhook_tx_id: str | None = None
    api_tx_id: str | None = None
    occurred_at: datetime | None = None

    def __post_init__(self) -> None:
        coerce_enum_fields(
            self, environment=Environment, direction=Direction, match_state=MatchState
        )


def transition_match_state(current: MatchState, target: MatchState) -> MatchState:
    """Return ``target`` when the match lifecycle allows ``current -> target``.

    ``in_review -> in_review`` is accepted as a no-op: a rematch that still needs review
    keeps the state and only replaces the review case.
    """
    current, target = MatchState(current), MatchState(target)
    if current == MatchState.IN_REVIEW and target == MatchState.IN_REVIEW:
        return target
    if target not in _MATCH_TRANSITIONS[current]:
        raise IllegalTransition("provider_transaction", current.value, target.value)
    return target
