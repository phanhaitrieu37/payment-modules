"""Unit of work: one database transaction and the repositories that share it.

Only the methods the processing pipeline relies on are declared here; the storage adapter
adds the rest of each repository.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from payment_module.domain.enums import (
    Direction,
    Environment,
    FirstSource,
    IdentityKind,
    InboxStatus,
    OutboxStatus,
    coerce_enum_fields,
)
from payment_module.domain.errors import DomainError
from payment_module.domain.events import JsonValue
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
class NewPaymentIntent:
    """Everything an intent snapshots except its ``payment_reference``, which storage asks
    the reference generator for so a collision can be retried with a new code."""

    tenant_id: str
    merchant_id: UUID
    environment: Environment
    receiving_account_id: UUID
    amount: AmountVnd
    beneficiary_snapshot: Mapping[str, JsonValue]
    reference_profile_version: int
    reference_prefix_name: str | None
    host_ref_type: str
    host_ref_id: str
    idempotency_key: str
    request_fingerprint: str
    expires_at: datetime

    def __post_init__(self) -> None:
        coerce_enum_fields(self, environment=Environment)


class IdempotencyConflict(DomainError):
    """The idempotency key is already used by an intent created from a different request."""


class ReferenceSpaceExhausted(DomainError):
    """Every generated reference collided with an existing one; the profile is too small."""


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
    async def create(
        self,
        new: NewPaymentIntent,
        generate_reference: Callable[[], str],
        max_attempts: int = 5,
    ) -> tuple[IntentView, bool]:
        """Insert the intent, or return the one already created for its idempotency key.

        Returns ``(intent, created)``. The same key with another ``request_fingerprint``
        raises :class:`IdempotencyConflict`; a reference collision retries with a new code
        up to ``max_attempts`` times, then raises :class:`ReferenceSpaceExhausted`.
        """
        ...

    async def get_by_idempotency_key(
        self, tenant_id: str, environment: Environment, idempotency_key: str
    ) -> IntentView | None: ...

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

    async def finalize(
        self,
        inbox_id: UUID,
        lease_generation: int,
        status: InboxStatus,
        *,
        next_attempt_at: datetime | None = None,
        last_error_code: str | None = None,
    ) -> bool:
        """Compare-and-set on ``lease_generation``; ``False`` when the lease was reclaimed."""
        ...


class OutboxRepository(Protocol):
    async def add(
        self,
        event: OutboxEventView,
        aggregate_type: str,
        aggregate_id: UUID,
        available_at: datetime,
    ) -> None: ...

    async def claim_batch(
        self, limit: int, lease_seconds: int, owner: str, now: datetime
    ) -> Sequence[ClaimedOutbox]: ...

    async def finalize(
        self,
        event_id: UUID,
        lease_generation: int,
        status: OutboxStatus,
        *,
        now: datetime,
        next_attempt_at: datetime | None = None,
        last_error: str | None = None,
    ) -> bool:
        """Compare-and-set on ``lease_generation``; ``False`` when the lease was reclaimed."""
        ...


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
