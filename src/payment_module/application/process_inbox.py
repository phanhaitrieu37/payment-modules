"""Worker side of the inbox: claim, process, and record failures, each in its own transaction.

1. **Claim** (short transaction, committed): ``processing`` with a new lease owner and
   ``lease_generation``.
2. **Process** (one transaction): re-lock the row under that generation, normalize, write
   the fact and its observation, match, apply the outcome, then finalize ``processed`` by
   compare-and-set on the generation. A lost lease rolls everything back.
3. **Failure** (new transaction, only after 2 rolled back): ``retry_wait`` with backoff,
   ``failed`` after the last attempt, again by compare-and-set. If this also fails, lease
   expiry lets another worker claim the row.

Lock order inside step 2 is fact, then intents (by id), then the review case; operator
review uses the same order.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from payment_module.application import provider_for
from payment_module.application.apply_match_outcome import ApplyMatchOutcome, HandlerFailed
from payment_module.application.config import PaymentModuleConfig, retry_delay
from payment_module.application.match_context import (
    link_by_bank_reference,
    load_match_context,
    resolve_receiver,
)
from payment_module.domain.enums import (
    FirstSource,
    IdentityKind,
    InboxStatus,
    LinkMethod,
    LinkStatus,
    MatchState,
    ObservationSource,
    ProcessingErrorCode,
    ReceiptTimeSource,
    SettlementOrigin,
)
from payment_module.domain.errors import DomainError, IllegalTransition, PolicyViolation
from payment_module.domain.matching.match_transaction import MatchTransaction
from payment_module.domain.reference import tokens_from
from payment_module.domain.review import MatchOutcome
from payment_module.domain.transaction import TransactionView
from payment_module.ports.clock import Clock
from payment_module.ports.handlers import TransactionOutcomeView
from payment_module.ports.metrics import MetricsSink, increment_safely
from payment_module.ports.provider import (
    NormalizedObservation,
    PaymentProvider,
    ReceivingAccountView,
    VerifiedDelivery,
)
from payment_module.ports.resolvers import ProviderConnection
from payment_module.ports.unit_of_work import (
    ClaimedInbox,
    NewProviderTransaction,
    UnitOfWork,
    UnitOfWorkFactory,
)

logger = logging.getLogger(__name__)

_REDECIDABLE = frozenset({MatchState.RECORDED, MatchState.IN_REVIEW})


class _ObservationConflict(Exception):
    """The observation's source id is already stored, but for a different fact."""


@dataclass(frozen=True, slots=True)
class _Handled:
    """What the processing transaction decided; alerts are emitted only after it commits."""

    status: InboxStatus
    error_code: ProcessingErrorCode | None = None
    view: TransactionOutcomeView | None = None
    outcome: MatchOutcome | None = None


class ProcessStatus(StrEnum):
    PROCESSED = "processed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class ProcessOneResult:
    """``processed``: this worker handled the row; ``reason`` is the inbox status it left.

    ``skipped``: nothing was done; ``reason`` says why (``already_claimed``, ``not_due``,
    ``not_found``, ``lease_lost`` or the row's final status).
    """

    status: ProcessStatus
    reason: str | None = None


def dedup_key(
    provider: str, provider_account_key: str, identity_kind: IdentityKind, identity_value: str
) -> str:
    """Canonical fact key, unique per tenant and environment."""
    return f"{provider}|{provider_account_key}|{IdentityKind(identity_kind).value}|{identity_value}"


@dataclass(frozen=True, slots=True)
class LinkedFact:
    """The locked canonical fact of a webhook sighting and how the sighting reached it."""

    tx: TransactionView
    created: bool
    link_method: LinkMethod


async def _link_or_create_fact(
    uow: UnitOfWork,
    connection: ProviderConnection,
    obs: NormalizedObservation,
    receiver: ReceivingAccountView | None,
    *,
    created_after: datetime | None,
) -> LinkedFact:
    """Find or create the canonical fact of a webhook observation; the fact stays locked.

    Under the scoped match-key lock (when the payload names a bank reference):

    1. a fact already carrying this webhook id is a replay (``same_source_id``); this runs
       before any insert because the webhook id may sit on a fact the API created;
    2. exactly one fact the API recorded with the same bank reference, account, direction
       and amount gets this webhook id (``bank_reference``) instead of a second fact;
    3. otherwise a new fact is inserted by its dedup key.
    """
    scope = (
        connection.tenant_id,
        connection.environment,
        connection.provider,
        obs.reported_account_key,
    )
    if obs.bank_reference is not None:
        await uow.transactions.lock_match_key(*scope, obs.bank_reference)
    existing = await uow.transactions.find_by_source_id(
        *scope, IdentityKind.WEBHOOK_ID, obs.source_tx_id
    )
    if existing is not None:
        return LinkedFact(existing, False, LinkMethod.SAME_SOURCE_ID)
    attempt = await link_by_bank_reference(
        uow,
        connection,
        reported_account_key=obs.reported_account_key,
        bank_reference=obs.bank_reference,
        direction=obs.direction,
        amount=obs.amount,
        source_tx_id=obs.source_tx_id,
        missing=IdentityKind.WEBHOOK_ID,
        created_after=created_after,
    )
    if attempt.tx is not None:
        return LinkedFact(attempt.tx, False, LinkMethod.BANK_REFERENCE)
    new = NewProviderTransaction(
        tenant_id=connection.tenant_id,
        environment=connection.environment,
        provider=connection.provider,
        provider_account_key=obs.reported_account_key,
        dedup_key=dedup_key(
            connection.provider,
            obs.reported_account_key,
            IdentityKind.WEBHOOK_ID,
            obs.source_tx_id,
        ),
        identity_kind=IdentityKind.WEBHOOK_ID,
        identity_value=obs.source_tx_id,
        receiving_account_id=None if receiver is None else receiver.id,
        merchant_id=None if receiver is None else receiver.merchant_id,
        amount=obs.amount,
        direction=obs.direction,
        bank_reference=obs.bank_reference,
        first_source=FirstSource.WEBHOOK,
        occurred_at=obs.provider_time,
    )
    tx, created = await uow.transactions.insert_or_get_by_dedup_key(new)
    if created:
        return LinkedFact(tx, True, LinkMethod.SAME_SOURCE_ID)
    locked = await uow.transactions.get_for_update(tx.tenant_id, tx.environment, tx.id)
    assert locked is not None
    return LinkedFact(locked, False, LinkMethod.SAME_SOURCE_ID)


class ProcessInbox:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        providers: Mapping[str, PaymentProvider],
        clock: Clock,
        match: MatchTransaction,
        apply_outcome: ApplyMatchOutcome,
        metrics: MetricsSink,
        config: PaymentModuleConfig,
    ) -> None:
        self._uow_factory = uow_factory
        self._providers = providers
        self._clock = clock
        self._match = match
        self._apply = apply_outcome
        self._metrics = metrics
        self._config = config
        self._owner = config.owner()

    async def run_batch(self, limit: int = 10) -> list[ProcessOneResult]:
        """Claim up to ``limit`` due rows (and expired leases) and process them in order."""
        async with self._uow_factory() as uow:
            claimed = await uow.inbox.claim_batch(
                limit, self._config.lease_seconds, self._owner, self._clock.now()
            )
            await uow.commit()
        return [await self.process_claimed(item) for item in claimed]

    async def process_one(self, inbox_id: UUID) -> ProcessOneResult:
        """Process one row now, for hosts that settle inline after the inbox commit.

        Never waits: a row a worker holds is reported as ``skipped/already_claimed``.
        """
        async with self._uow_factory() as uow:
            claimed = await uow.inbox.claim_one(
                inbox_id, self._config.lease_seconds, self._owner, self._clock.now()
            )
            if claimed is None:
                status = await uow.inbox.get_status(inbox_id)
            await uow.commit()
        if claimed is not None:
            return await self.process_claimed(claimed)
        match status:
            case None:
                reason = "not_found"
            case InboxStatus.RECEIVED | InboxStatus.PROCESSING:
                reason = "already_claimed"
            case InboxStatus.RETRY_WAIT:
                reason = "not_due"
            case _:
                reason = status.value
        return ProcessOneResult(ProcessStatus.SKIPPED, reason)

    async def process_claimed(self, claimed: ClaimedInbox) -> ProcessOneResult:
        """Process a row this worker claimed; safe to call after the lease was reclaimed.

        Every claim counts as an attempt, including one whose worker died before it could
        record anything. A row claimed more than ``inbox_max_attempts`` times is failed
        here, before any processing, so a delivery that crashes the worker cannot loop.
        """
        if claimed.attempts > self._config.inbox_max_attempts:
            logger.error(
                "payment_inbox_failed",
                extra={"inbox_id": str(claimed.id), "error": "max_attempts_exceeded"},
            )
            return await self._record_failure(
                claimed, InboxStatus.FAILED, ProcessingErrorCode.MAX_ATTEMPTS_EXCEEDED
            )
        try:
            async with self._uow_factory() as uow:
                if not await uow.inbox.lock_claim(claimed.id, claimed.lease_generation):
                    return self._lease_lost(claimed)
                handled = await self._handle(uow, claimed)
                finalized = await uow.inbox.finalize(
                    claimed.id,
                    claimed.lease_generation,
                    handled.status,
                    last_error_code=_code(handled.error_code),
                )
                if not finalized:
                    await uow.rollback()
                    return self._lease_lost(claimed)
                await uow.commit()
        except _ObservationConflict:
            logger.error("payment_inbox_observation_conflict", extra={"inbox_id": str(claimed.id)})
            return await self._record_failure(
                claimed, InboxStatus.FAILED, ProcessingErrorCode.OBSERVATION_CONFLICT
            )
        except (PolicyViolation, IllegalTransition) as exc:
            logger.warning(
                "payment_inbox_invariant_violation",
                extra={"inbox_id": str(claimed.id), "error": type(exc).__name__},
            )
            return await self._record_failure(
                claimed, InboxStatus.FAILED, ProcessingErrorCode.POLICY_VIOLATION
            )
        except Exception as exc:
            return await self._retry_or_fail(claimed, exc)
        self._after_commit(claimed, handled)
        return ProcessOneResult(ProcessStatus.PROCESSED, handled.status.value)

    async def _retry_or_fail(self, claimed: ClaimedInbox, exc: Exception) -> ProcessOneResult:
        failed = exc.__cause__ if isinstance(exc, HandlerFailed) else exc
        code = (
            ProcessingErrorCode.HANDLER_ERROR
            if isinstance(exc, HandlerFailed)
            else ProcessingErrorCode.TRANSIENT_ERROR
        )
        fields = {
            "inbox_id": str(claimed.id),
            "error": type(failed).__name__,
            "code": code.value,
            "attempts": claimed.attempts,
        }
        if claimed.attempts >= self._config.inbox_max_attempts:
            logger.error("payment_inbox_failed", extra=fields)
            return await self._record_failure(claimed, InboxStatus.FAILED, code)
        logger.warning("payment_inbox_retry", extra=fields)
        next_attempt_at = self._clock.now() + retry_delay(claimed.attempts)
        return await self._record_failure(claimed, InboxStatus.RETRY_WAIT, code, next_attempt_at)

    def _after_commit(self, claimed: ClaimedInbox, handled: _Handled) -> None:
        if handled.status == InboxStatus.QUARANTINED:
            increment_safely(
                self._metrics,
                "inbox_quarantined_total",
                {"tenant_id": claimed.tenant_id, "reason": _code(handled.error_code) or ""},
            )
        if handled.view is not None and handled.outcome is not None:
            self._apply.after_commit(handled.view, handled.outcome)

    async def _handle(self, uow: UnitOfWork, claimed: ClaimedInbox) -> _Handled:
        connection = await uow.connections.get(claimed.tenant_id, claimed.connection_id)
        if connection is None:
            raise LookupError(f"connection of inbox row {claimed.id} not found")
        provider = provider_for(self._providers, connection.provider)
        delivery = VerifiedDelivery(claimed.raw_body, claimed.headers, claimed.received_at)
        try:
            obs = provider.normalize(delivery)
        except (DomainError, ValueError):
            logger.warning("payment_inbox_quarantined", extra={"inbox_id": str(claimed.id)})
            return _Handled(InboxStatus.QUARANTINED, ProcessingErrorCode.NORMALIZE_FAILED)

        receiver = await self._receiver(uow, connection, obs)
        linked = await _link_or_create_fact(
            uow,
            connection,
            obs,
            receiver,
            created_after=claimed.received_at
            - timedelta(hours=self._config.reconcile_window_hours),
        )
        tx, created = linked.tx, linked.created
        observation_id = await uow.observations.add_if_absent(
            tenant_id=connection.tenant_id,
            environment=connection.environment,
            connection_id=connection.id,
            provider=connection.provider,
            source=ObservationSource.WEBHOOK,
            source_tx_id=obs.source_tx_id,
            reported_account_key=obs.reported_account_key,
            amount=obs.amount,
            direction=obs.direction,
            observed_at=self._clock.now(),
            inbox_id=claimed.id,
            bank_reference=obs.bank_reference,
            occurred_at=obs.provider_time,
            code=obs.code,
            memo=obs.content,
            transaction_id=tx.id,
            link_method=linked.link_method,
            link_status=LinkStatus.LINKED,
            purge_after=self._config.purge_after(claimed.received_at),
        )
        if observation_id is None and created:
            # Same source id, different account or payload: never merge money silently.
            raise _ObservationConflict
        if (
            linked.link_method == LinkMethod.SAME_SOURCE_ID
            and not created
            and (tx.amount != obs.amount or tx.direction != obs.direction)
        ):
            # Same source id and account, different signed amount or direction: the first
            # sighting stays the fact, but an operator should see the disagreement.
            logger.warning(
                "payment_observation_mismatch",
                extra={"inbox_id": str(claimed.id), "transaction_id": str(tx.id)},
            )
        if observation_id is None:
            return _Handled(InboxStatus.PROCESSED)
        if linked.link_method == LinkMethod.BANK_REFERENCE:
            # A signed webhook for money the API recorded first: decide again, unless that
            # fact already reached a final state (settled, not applicable, closed).
            if tx.match_state not in _REDECIDABLE:
                return _Handled(InboxStatus.PROCESSED)
        elif tx.match_state != MatchState.RECORDED:
            # A replay of money already seen: the outcome is never applied twice.
            return _Handled(InboxStatus.PROCESSED)

        outcome, view = await self._decide(uow, connection, obs, tx, claimed.received_at)
        return _Handled(InboxStatus.PROCESSED, view=view, outcome=outcome)

    @staticmethod
    async def _receiver(
        uow: UnitOfWork, connection: ProviderConnection, obs: NormalizedObservation
    ) -> ReceivingAccountView | None:
        return await resolve_receiver(uow, connection, obs.reported_account_key)

    async def _decide(
        self,
        uow: UnitOfWork,
        connection: ProviderConnection,
        obs: NormalizedObservation,
        tx: TransactionView,
        received_at: datetime,
    ) -> tuple[MatchOutcome, TransactionOutcomeView]:
        ctx, intents = await load_match_context(
            uow,
            tx=tx,
            connection=connection,
            tokens=tokens_from(obs.code, obs.content),
            effective_received_at=received_at,
            time_source=ReceiptTimeSource.WEBHOOK_RECEIVED,
        )
        outcome = self._match.decide(ctx)
        view = await self._apply.apply(
            uow, tx=tx, outcome=outcome, intents=intents, origin=SettlementOrigin.AUTO
        )
        return outcome, view

    async def _record_failure(
        self,
        claimed: ClaimedInbox,
        status: InboxStatus,
        error_code: ProcessingErrorCode,
        next_attempt_at: datetime | None = None,
    ) -> ProcessOneResult:
        try:
            async with self._uow_factory() as uow:
                recorded = await uow.inbox.finalize(
                    claimed.id,
                    claimed.lease_generation,
                    status,
                    next_attempt_at=next_attempt_at,
                    last_error_code=error_code.value,
                )
                await uow.commit()
        except Exception as exc:
            # Lease expiry is the recovery path when even the failure update cannot commit.
            logger.error(
                "payment_inbox_failure_not_recorded",
                extra={"inbox_id": str(claimed.id), "error": type(exc).__name__},
            )
            return ProcessOneResult(ProcessStatus.SKIPPED, "failure_not_recorded")
        if not recorded:
            return self._lease_lost(claimed)
        if status == InboxStatus.FAILED:
            increment_safely(
                self._metrics,
                "inbox_failed_total",
                {"tenant_id": claimed.tenant_id, "code": error_code.value},
            )
        return ProcessOneResult(ProcessStatus.PROCESSED, status.value)

    @staticmethod
    def _lease_lost(claimed: ClaimedInbox) -> ProcessOneResult:
        logger.warning(
            "payment_inbox_lease_lost",
            extra={"inbox_id": str(claimed.id), "lease_generation": claimed.lease_generation},
        )
        return ProcessOneResult(ProcessStatus.SKIPPED, "lease_lost")


def _code(code: ProcessingErrorCode | None) -> str | None:
    return None if code is None else code.value
