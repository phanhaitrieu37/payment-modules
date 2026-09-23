"""Unit of work: one database transaction and the repositories that share it.

Only the methods the processing pipeline relies on are declared here; the storage adapter
adds the rest of each repository.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from payment_module.domain.enums import (
    ConnectionStatus,
    Direction,
    Environment,
    EventKeyKind,
    FirstSource,
    IdentityKind,
    InboxStatus,
    IntentStatus,
    LinkMethod,
    LinkStatus,
    MatchState,
    MerchantStatus,
    ObservationSource,
    OutboxStatus,
    ProfileStatus,
    ReadinessStatus,
    ReceivingAccountStatus,
    ReconcileMode,
    ReconciliationRunStatus,
    ReviewCaseStatus,
    ReviewReason,
    ReviewResolution,
    SettlementOrigin,
    coerce_enum_fields,
)
from payment_module.domain.events import JsonValue
from payment_module.domain.intent import IntentView
from payment_module.domain.money import AmountVnd
from payment_module.domain.reference import ReferenceProfile
from payment_module.domain.transaction import TransactionView
from payment_module.ports.provider import ReceivingAccountView
from payment_module.ports.publisher import OutboxEventView
from payment_module.ports.resolvers import ProviderConnection


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
    occurred_at: datetime | None = None

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
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ClaimedOutbox:
    event: OutboxEventView
    attempts: int
    lease_generation: int


@dataclass(frozen=True, slots=True)
class MerchantView:
    id: UUID
    tenant_id: str
    host_merchant_ref: str
    status: MerchantStatus

    def __post_init__(self) -> None:
        coerce_enum_fields(self, status=MerchantStatus)


@dataclass(frozen=True, slots=True)
class ObservationView:
    """One sighting of a transaction by a source, as stored."""

    id: UUID
    tenant_id: str
    environment: Environment
    connection_id: UUID
    source: ObservationSource
    source_tx_id: str
    reported_account_key: str
    amount: AmountVnd
    direction: Direction
    bank_reference: str | None
    code: str | None
    memo: str | None
    occurred_at: datetime | None
    observed_at: datetime
    transaction_id: UUID | None
    link_method: LinkMethod | None
    link_status: LinkStatus

    def __post_init__(self) -> None:
        coerce_enum_fields(
            self,
            environment=Environment,
            source=ObservationSource,
            direction=Direction,
            link_method=LinkMethod,
            link_status=LinkStatus,
        )


@dataclass(frozen=True, slots=True)
class ReviewCaseView:
    id: UUID
    tenant_id: str
    environment: Environment
    transaction_id: UUID
    candidate_intent_id: UUID | None
    reason: ReviewReason
    details: Mapping[str, JsonValue]
    status: ReviewCaseStatus
    resolution: ReviewResolution | None
    resolution_ref: str | None
    resolved_by: str | None
    resolution_note: str | None
    opened_at: datetime
    resolved_at: datetime | None

    def __post_init__(self) -> None:
        coerce_enum_fields(
            self,
            environment=Environment,
            reason=ReviewReason,
            status=ReviewCaseStatus,
            resolution=ReviewResolution,
        )


@dataclass(frozen=True, slots=True)
class ReadinessView:
    """Readiness of one connection for one profile version in one environment.

    ``confirmed`` holds the checklist keys the operator confirmed when it was recorded.
    """

    id: UUID
    tenant_id: str
    connection_id: UUID
    profile_version: int
    environment: Environment
    status: ReadinessStatus
    confirmed: frozenset[str]
    evidence_ref: str | None
    verified_by: str | None
    verified_at: datetime | None

    def __post_init__(self) -> None:
        coerce_enum_fields(self, environment=Environment, status=ReadinessStatus)


@dataclass(frozen=True, slots=True)
class ReadTarget:
    """One account bound to a connection, and its provider-side id for API reads."""

    receiving_account_id: UUID
    provider_account_ref: str | None


@dataclass(frozen=True, slots=True)
class RunCheckpoint:
    """Where the last completed read of one account stopped inside its window."""

    window_from: datetime
    window_to: datetime
    cursor: str


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

    async def find_by_idempotency_key(
        self, tenant_id: str, environment: Environment, idempotency_key: str
    ) -> tuple[IntentView, str] | None:
        """The intent created for this key and its ``request_fingerprint``."""
        ...

    async def get_for_update(self, tenant_id: str, intent_id: UUID) -> IntentView | None: ...

    async def get(self, tenant_id: str, intent_id: UUID) -> IntentView | None: ...

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

    async def mark_paid(self, tenant_id: str, intent_id: UUID, paid_at: datetime) -> bool:
        """``awaiting_payment | expired -> paid``; ``False`` when the intent is in neither."""
        ...

    async def set_status(
        self,
        tenant_id: str,
        intent_id: UUID,
        status: IntentStatus,
        *,
        cancel_reason: str | None,
        superseded_by_intent_id: UUID | None,
    ) -> bool:
        """``awaiting_payment -> status``; ``False`` when the intent already moved on."""
        ...

    async def count_awaiting_by_profile_version(self, version: int) -> int:
        """Intents of ``version`` still ``awaiting_payment``."""
        ...

    async def count_settleable_by_profile_version(
        self, version: int, expired_after: datetime
    ) -> int:
        """Intents of ``version`` still ``awaiting_payment``, or ``expired`` with
        ``expires_at >= expired_after``."""
        ...


class TransactionRepository(Protocol):
    async def insert_or_get_by_dedup_key(
        self, new: NewProviderTransaction
    ) -> tuple[TransactionView, bool]:
        """Return the canonical fact and whether this call created it."""
        ...

    async def get_for_update(
        self, tenant_id: str, environment: Environment, transaction_id: UUID
    ) -> TransactionView | None:
        """Read the fact and lock its row for the rest of the transaction."""
        ...

    async def set_match_state(
        self, transaction_id: UUID, expected: MatchState, target: MatchState
    ) -> bool:
        """Compare-and-set on ``match_state``; ``False`` when it was not ``expected``."""
        ...

    async def lock_match_key(
        self,
        tenant_id: str,
        environment: Environment,
        provider: str,
        provider_account_key: str,
        bank_reference: str,
    ) -> None:
        """Serialize link-or-create of one scoped bank reference until the transaction ends."""
        ...

    async def find_by_source_id(
        self,
        tenant_id: str,
        environment: Environment,
        provider: str,
        provider_account_key: str,
        identity_kind: IdentityKind,
        value: str,
    ) -> TransactionView | None:
        """The fact already carrying this webhook or API id, locked ``FOR UPDATE``."""
        ...

    async def find_linkable_by_bank_reference(
        self,
        tenant_id: str,
        environment: Environment,
        provider: str,
        provider_account_key: str,
        bank_reference: str,
        direction: Direction,
        amount: AmountVnd,
        *,
        missing: IdentityKind,
        created_after: datetime | None = None,
    ) -> Sequence[TransactionView]:
        """Same-scope facts with this bank reference, direction and amount that lack an id of
        kind ``missing``, locked ``FOR UPDATE``."""
        ...

    async def attach_source_id(
        self, transaction_id: UUID, identity_kind: IdentityKind, value: str
    ) -> bool:
        """Record the other source's id; ``False`` when the fact already has one."""
        ...

    async def purge_expired(self, now: datetime, limit: int, *, linkable_after: datetime) -> int:
        """Clear ``memo`` of up to ``limit`` facts whose ``purge_after`` is before ``now``,
        once the fact is decided (final match state, no open review case) and can no longer
        be linked: it carries both source ids, or it and all its observations predate
        ``linkable_after``.

        Identity, dedup and amount columns are never touched. Returns the rows purged.
        """
        ...


class InboxRepository(Protocol):
    async def add(
        self,
        *,
        tenant_id: str,
        connection_id: UUID,
        event_key: str,
        event_key_kind: EventKeyKind,
        body_sha256: str,
        raw_body: bytes,
        headers: Mapping[str, str],
        received_at: datetime,
        status: InboxStatus = InboxStatus.RECEIVED,
        last_error_code: str | None = None,
        purge_after: datetime | None = None,
    ) -> UUID | None:
        """Insert a delivery; ``None`` when ``(connection_id, event_key)`` already exists."""
        ...

    async def find_id(self, connection_id: UUID, event_key: str) -> UUID | None: ...

    async def get_status(self, inbox_id: UUID) -> InboxStatus | None:
        """Plain read that never waits on a lock."""
        ...

    async def claim_batch(
        self, limit: int, lease_seconds: int, owner: str, now: datetime
    ) -> Sequence[ClaimedInbox]: ...

    async def claim_one(
        self, inbox_id: UUID, lease_seconds: int, owner: str, now: datetime
    ) -> ClaimedInbox | None:
        """Claim this row if it is due and not locked by another claim (``SKIP LOCKED``)."""
        ...

    async def lock_claim(self, inbox_id: UUID, lease_generation: int) -> bool:
        """Lock the row ``FOR UPDATE`` while it is still ``processing`` under this lease."""
        ...

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

    async def requeue(self, inbox_id: UUID) -> bool:
        """``failed | quarantined -> received`` with attempts reset; ``False`` otherwise."""
        ...

    async def purge_expired(self, now: datetime, limit: int) -> int:
        """Clear ``raw_body`` and ``headers`` and set ``raw_purged_at = now`` on up to
        ``limit`` ``processed | failed | quarantined`` rows whose ``purge_after`` is before
        ``now``. Rows still waiting for the worker are kept. Returns the rows purged."""
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

    async def requeue(self, event_id: UUID) -> bool:
        """``failed -> pending`` with attempts reset; ``False`` otherwise."""
        ...


class MerchantRepository(Protocol):
    async def add(
        self,
        tenant_id: str,
        host_merchant_ref: str,
        status: MerchantStatus = MerchantStatus.ACTIVE,
    ) -> UUID: ...

    async def get(self, tenant_id: str, merchant_id: UUID) -> MerchantView | None: ...

    async def get_id_by_host_ref(self, tenant_id: str, host_merchant_ref: str) -> UUID | None: ...


class ReceivingAccountRepository(Protocol):
    async def add(
        self,
        *,
        tenant_id: str,
        merchant_id: UUID,
        environment: Environment,
        bank_code: str,
        bank_bin: str | None,
        account_number: str,
        sub_account: str | None,
        account_number_masked: str,
        holder_name: str,
        account_fingerprint: str,
        provider_account_ref: str | None = None,
    ) -> UUID: ...

    async def get(self, tenant_id: str, account_id: UUID) -> ReceivingAccountView | None: ...

    async def find_by_fingerprint(
        self, environment: Environment, account_fingerprint: str
    ) -> ReceivingAccountView | None: ...

    async def fingerprints(self, account_ids: Collection[UUID]) -> dict[UUID, str]:
        """``account_fingerprint`` of each given account."""
        ...

    async def set_status(
        self, tenant_id: str, account_id: UUID, status: ReceivingAccountStatus
    ) -> bool: ...


class ConnectionRepository(Protocol):
    async def add(
        self,
        *,
        tenant_id: str,
        merchant_id: UUID,
        provider: str,
        environment: Environment,
        locator: str,
        secret_ref: str,
        api_credential_ref: str | None = None,
    ) -> UUID: ...

    async def get(self, tenant_id: str, connection_id: UUID) -> ProviderConnection | None: ...

    async def get_for_update(
        self, tenant_id: str, connection_id: UUID
    ) -> ProviderConnection | None: ...

    async def by_locator(self, locator: str) -> ProviderConnection | None: ...

    async def list_active(self) -> Sequence[ProviderConnection]:
        """``active`` connections of every tenant, in ``id`` order."""
        ...

    async def set_status(
        self,
        tenant_id: str,
        connection_id: UUID,
        status: ConnectionStatus,
        *,
        changed_by: str,
        changed_at: datetime,
        reason: str,
    ) -> bool: ...

    async def set_reconcile_mode(
        self,
        tenant_id: str,
        connection_id: UUID,
        mode: ReconcileMode,
        evidence_ref: str | None,
    ) -> bool: ...

    async def list_reconcilable(self) -> Sequence[ProviderConnection]:
        """Connections of every tenant with an API credential that are not ``disabled``."""
        ...


class ConnectionBindingRepository(Protocol):
    async def add(
        self,
        *,
        tenant_id: str,
        merchant_id: UUID,
        environment: Environment,
        connection_id: UUID,
        receiving_account_id: UUID,
        created_by: str,
    ) -> None: ...

    async def delete_for_account(self, receiving_account_id: UUID) -> list[UUID]:
        """Remove every binding of the account; returns the connections it was bound to."""
        ...

    async def account_ids(self, connection_id: UUID) -> list[UUID]: ...

    async def connection_ids_for_account(self, receiving_account_id: UUID) -> list[UUID]: ...


class ReferenceProfileRepository(Protocol):
    async def add(self, profile: ReferenceProfile) -> None: ...

    async def get(self, version: int) -> ReferenceProfile | None: ...

    async def get_active(self) -> ReferenceProfile | None: ...

    async def lock_all(self) -> Sequence[ReferenceProfile]:
        """Every profile, locked ``FOR UPDATE`` in version order."""
        ...

    async def share_all(self) -> Sequence[ReferenceProfile]:
        """Every profile, locked ``FOR SHARE`` in version order.

        Taken before a connection lock by every path that makes a connection ``active``, so
        such a path and a profile activation never interleave and never deadlock.
        """
        ...

    async def set_status(
        self, version: int, status: ProfileStatus, *, actor: str, at: datetime
    ) -> None:
        """Stamps ``activated_*`` or ``retired_*`` when moving to those states."""
        ...

    async def list_by_status(self, statuses: Collection[ProfileStatus]) -> list[ReferenceProfile]:
        """Profiles in any of ``statuses``, in version order."""
        ...


class ReadinessRepository(Protocol):
    async def get(
        self, connection_id: UUID, profile_version: int, environment: Environment
    ) -> ReadinessView | None: ...

    async def upsert(
        self,
        *,
        tenant_id: str,
        connection_id: UUID,
        profile_version: int,
        environment: Environment,
        status: ReadinessStatus,
        checklist: Mapping[str, JsonValue],
        evidence_ref: str | None,
        verified_by: str | None,
        verified_at: datetime | None,
    ) -> ReadinessView:
        """Insert or replace the row of ``(connection_id, profile_version, environment)``."""
        ...

    async def list_missing_for(self, version: int, environment: Environment) -> list[UUID]:
        """``active`` connections of ``environment`` without ``ready`` readiness for
        ``version``, in ``id`` order."""
        ...

    async def invalidate_for_connection(self, connection_id: UUID) -> int:
        """Every ``ready`` row of the connection back to ``pending``; returns the count."""
        ...

    async def invalidate_for_version(self, version: int) -> int:
        """Every ``ready`` row of ``version`` back to ``pending``; returns the count."""
        ...

    async def retire_for_version(self, version: int) -> int:
        """Every row of ``version`` to ``retired``; returns the count."""
        ...


class ObservationRepository(Protocol):
    async def add_if_absent(
        self,
        *,
        tenant_id: str,
        environment: Environment,
        connection_id: UUID,
        provider: str,
        source: ObservationSource,
        source_tx_id: str,
        reported_account_key: str,
        amount: AmountVnd,
        direction: Direction,
        observed_at: datetime,
        inbox_id: UUID | None = None,
        reconciliation_run_id: UUID | None = None,
        bank_reference: str | None = None,
        occurred_at: datetime | None = None,
        code: str | None = None,
        memo: str | None = None,
        transaction_id: UUID | None = None,
        link_method: LinkMethod | None = None,
        link_status: LinkStatus = LinkStatus.UNLINKED,
        purge_after: datetime | None = None,
    ) -> UUID | None:
        """Insert an observation; ``None`` when its source id is already stored."""
        ...

    async def list_for_transaction(self, transaction_id: UUID) -> Sequence[ObservationView]:
        """Every observation linked to the fact, oldest ``observed_at`` first."""
        ...

    async def list_unlinked_before(
        self, connection_id: UUID, observed_before: datetime, limit: int
    ) -> Sequence[UUID]:
        """API observations without a fact (``unlinked`` or ``ambiguous``) first seen before
        ``observed_before``."""
        ...

    async def get_unlinked_for_update(self, observation_id: UUID) -> ObservationView | None:
        """The observation while it has no fact yet, locked; ``None`` if held or linked."""
        ...

    async def set_link(
        self,
        observation_id: UUID,
        transaction_id: UUID | None,
        link_method: LinkMethod | None,
        link_status: LinkStatus,
    ) -> bool:
        """Link or mark ambiguous an observation without a fact; ``False`` otherwise."""
        ...

    async def purge_expired(self, now: datetime, limit: int, *, linkable_after: datetime) -> int:
        """Clear ``memo`` and ``normalized`` of up to ``limit`` observations whose
        ``purge_after`` is before ``now`` and whose fact is decided (final match state, no
        open review case) and can no longer be linked (both source ids, or the fact and all
        its observations predate ``linkable_after``); unlinked observations wait. Returns
        the rows purged."""
        ...


class SettlementRepository(Protocol):
    async def add(
        self,
        *,
        tenant_id: str,
        environment: Environment,
        transaction_id: UUID,
        intent_id: UUID,
        receiving_account_id: UUID,
        amount: AmountVnd,
        intent_amount: AmountVnd,
        origin: SettlementOrigin,
        settled_at: datetime,
        review_case_id: UUID | None = None,
        resolved_by: str | None = None,
    ) -> UUID: ...


class ReviewCaseRepository(Protocol):
    async def open(
        self,
        *,
        tenant_id: str,
        environment: Environment,
        transaction_id: UUID,
        reason: ReviewReason,
        details: Mapping[str, str],
        opened_at: datetime,
        candidate_intent_id: UUID | None = None,
    ) -> UUID: ...

    async def find_open(self, transaction_id: UUID) -> UUID | None:
        """Id of the fact's open case, locked ``FOR UPDATE``."""
        ...

    async def update_open(
        self,
        case_id: UUID,
        reason: ReviewReason,
        details: Mapping[str, str],
        candidate_intent_id: UUID | None,
    ) -> bool: ...

    async def get(self, tenant_id: str, case_id: UUID) -> ReviewCaseView | None: ...

    async def get_for_update(self, case_id: UUID) -> ReviewCaseView | None: ...

    async def resolve(
        self,
        case_id: UUID,
        *,
        resolution: ReviewResolution,
        resolved_by: str,
        resolution_ref: str | None,
        resolution_note: str | None,
        resolved_at: datetime,
        details: Mapping[str, JsonValue] | None = None,
    ) -> bool:
        """``open -> resolved``; ``details`` replaces the stored details when given."""
        ...

    async def list_open(
        self,
        tenant_id: str,
        reasons: Collection[ReviewReason],
        *,
        connection_id: UUID | None = None,
        opened_after: datetime | None = None,
    ) -> Sequence[ReviewCaseView]:
        """Open cases with one of ``reasons``, oldest first.

        ``connection_id`` keeps only facts observed through that connection.
        """
        ...


class ReconciliationRunRepository(Protocol):
    async def start(
        self,
        *,
        tenant_id: str,
        connection_id: UUID,
        window_from: datetime,
        window_to: datetime,
        started_at: datetime,
        cursor: str | None = None,
        account_ref: str | None = None,
    ) -> UUID: ...

    async def finish(
        self,
        run_id: UUID,
        *,
        status: ReconciliationRunStatus,
        counts: Mapping[str, JsonValue],
        cursor: str | None,
        finished_at: datetime,
        last_error: str | None = None,
    ) -> None: ...

    async def last_checkpoint(self, connection_id: UUID, account_ref: str) -> RunCheckpoint | None:
        """Window and next cursor of the account's last completed run, if it left one."""
        ...

    async def read_targets(self, connection_id: UUID) -> Sequence[ReadTarget]:
        """Accounts bound to the connection, with their provider-side reference."""
        ...


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

    async def __aenter__(self) -> Self:
        """Begin the transaction (a savepoint when joined to a host transaction)."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Roll back whatever was not committed and release the connection."""
        ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...


type UnitOfWorkFactory = Callable[[], UnitOfWork]
