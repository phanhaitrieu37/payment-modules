"""Unit of work: one database transaction and the repositories that share it.

Only the methods the processing pipeline relies on are declared here; the storage adapter
adds the rest of each repository.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from payment_module.domain.enums import (
    Direction,
    Environment,
    FirstSource,
    IdentityKind,
    coerce_enum_fields,
)
from payment_module.domain.intent import IntentView
from payment_module.domain.money import AmountVnd
from payment_module.domain.transaction import TransactionView
from payment_module.ports.publisher import OutboxEventView


@dataclass(frozen=True, slots=True)
class NewProviderTransaction:
    tenant_id: str
    environment: Environment
    provider: str
    provider_account_key: str
    dedup_key: str
    identity_kind: IdentityKind
    identity_value: str
    receiving_account_id: UUID | None
    merchant_id: UUID | None
    amount: AmountVnd
    direction: Direction
    bank_reference: str | None
    first_source: FirstSource

    def __post_init__(self) -> None:
        coerce_enum_fields(
            self,
            environment=Environment,
            identity_kind=IdentityKind,
            direction=Direction,
            first_source=FirstSource,
        )


@dataclass(frozen=True, slots=True)
class ClaimedInbox:
    id: UUID
    tenant_id: str
    connection_id: UUID
    event_key: str
    raw_body: bytes
    received_at: datetime
    attempts: int
    lease_generation: int


@dataclass(frozen=True, slots=True)
class ClaimedOutbox:
    event: OutboxEventView
    attempts: int
    lease_generation: int


class IntentRepository(Protocol):
    async def get_for_update(self, tenant_id: str, intent_id: UUID) -> IntentView | None: ...

    async def find_by_references_for_update(
        self, payment_references: Collection[str]
    ) -> Sequence[IntentView]:
        """Intents whose ``payment_reference`` is in ``payment_references``, locked ``FOR UPDATE``.

        Deliberately **no tenant filter**: ``payment_reference`` is unique per project, so a
        memo can name an intent of another tenant or environment. That hit must reach the
        core scope check and become ``TENANT_MISMATCH``; filtering by tenant here would hide
        it as ``NO_REFERENCE``. Rows are locked in ``id`` order to avoid lock-order deadlocks.
        """
        ...


class TransactionRepository(Protocol):
    async def insert_or_get_by_dedup_key(
        self, new: NewProviderTransaction
    ) -> tuple[TransactionView, bool]:
        """Return the canonical fact and whether this call created it."""
        ...


class InboxRepository(Protocol):
    async def claim_batch(
        self, limit: int, lease_seconds: int, owner: str, now: datetime
    ) -> Sequence[ClaimedInbox]: ...


class OutboxRepository(Protocol):
    async def claim_batch(
        self, limit: int, lease_seconds: int, owner: str, now: datetime
    ) -> Sequence[ClaimedOutbox]: ...


class MerchantRepository(Protocol): ...


class ReceivingAccountRepository(Protocol): ...


class ConnectionRepository(Protocol): ...


class ConnectionBindingRepository(Protocol): ...


class ReferenceProfileRepository(Protocol): ...


class ReadinessRepository(Protocol): ...


class ObservationRepository(Protocol): ...


class SettlementRepository(Protocol): ...


class ReviewCaseRepository(Protocol): ...


class ReconciliationRunRepository(Protocol): ...


class UnitOfWork(Protocol):
    merchants: MerchantRepository
    receiving_accounts: ReceivingAccountRepository
    connections: ConnectionRepository
    connection_bindings: ConnectionBindingRepository
    reference_profiles: ReferenceProfileRepository
    readiness: ReadinessRepository
    intents: IntentRepository
    inbox: InboxRepository
    observations: ObservationRepository
    transactions: TransactionRepository
    settlements: SettlementRepository
    review_cases: ReviewCaseRepository
    outbox: OutboxRepository
    reconciliation_runs: ReconciliationRunRepository

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...
