"""First core step: money direction, then receiver and merchant scope of the connection."""

from __future__ import annotations

from collections.abc import Set
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from payment_module.domain.enums import Direction, Environment
from payment_module.domain.transaction import TransactionView


@dataclass(frozen=True, slots=True)
class ConnectionView:
    """Trusted scope of the connection the fact arrived through."""

    id: UUID
    tenant_id: str
    merchant_id: UUID
    environment: Environment


class GuardResult(StrEnum):
    PASS = "PASS"
    OUTGOING = "OUTGOING"
    RECEIVER_UNBOUND = "RECEIVER_UNBOUND"


class InvariantGuard:
    """Checks that cannot be replaced or relaxed by a host policy.

    Direction is checked first: outgoing and unknown-direction money is never a payment,
    whatever its receiver or source. Incoming money passes only when its receiving account
    is bound to this connection and fact, connection and account share tenant, merchant and
    environment; anything else is treated as an unbound receiver.
    """

    def check(
        self,
        tx: TransactionView,
        bound_account_ids: Set[UUID],
        connection: ConnectionView,
    ) -> GuardResult:
        if tx.direction is not Direction.IN:
            return GuardResult.OUTGOING
        in_scope = (
            tx.receiving_account_id is not None
            and tx.receiving_account_id in bound_account_ids
            and tx.tenant_id == connection.tenant_id
            and tx.environment is connection.environment
            and tx.merchant_id == connection.merchant_id
        )
        return GuardResult.PASS if in_scope else GuardResult.RECEIVER_UNBOUND
