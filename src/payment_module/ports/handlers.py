"""Host hooks that run inside the settlement transaction.

Both receive the caller's unit of work. They must not commit, open another transaction or
call the network; raising rolls back the whole decision and the inbox row is retried.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from payment_module.domain.enums import (
    Direction,
    Environment,
    MatchState,
    ReviewReason,
    SettlementOrigin,
    coerce_enum_fields,
)
from payment_module.domain.money import AmountVnd
from payment_module.ports.unit_of_work import UnitOfWork


@dataclass(frozen=True, slots=True)
class SettlementView:
    settlement_id: UUID
    tenant_id: str
    environment: Environment
    merchant_id: UUID
    intent_id: UUID
    transaction_id: UUID
    receiving_account_id: UUID
    amount: AmountVnd
    origin: SettlementOrigin
    settled_at: datetime
    host_ref_type: str
    host_ref_id: str

    def __post_init__(self) -> None:
        coerce_enum_fields(self, environment=Environment, origin=SettlementOrigin)


@dataclass(frozen=True, slots=True)
class TransactionOutcomeView:
    """Immutable result of one fact's matching, for synchronous host projections."""

    transaction_id: UUID
    tenant_id: str
    environment: Environment
    merchant_id: UUID | None
    match_state: MatchState
    direction: Direction
    amount: AmountVnd
    intent_id: UUID | None
    review_case_id: UUID | None
    review_reason: ReviewReason | None

    def __post_init__(self) -> None:
        coerce_enum_fields(
            self,
            environment=Environment,
            match_state=MatchState,
            direction=Direction,
            review_reason=ReviewReason,
        )


class SettlementHandler(Protocol):
    async def on_settled(self, uow: UnitOfWork, settled: SettlementView) -> None: ...


class OutcomeObserver(Protocol):
    async def on_outcome(self, uow: UnitOfWork, outcome: TransactionOutcomeView) -> None: ...


class NoOpOutcomeObserver:
    async def on_outcome(self, uow: UnitOfWork, outcome: TransactionOutcomeView) -> None:
        return None


class NoOpSettlementHandler:
    async def on_settled(self, uow: UnitOfWork, settled: SettlementView) -> None:
        return None
