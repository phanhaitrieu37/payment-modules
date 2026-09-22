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
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from payment_module.application import provider_for
from payment_module.application.apply_match_outcome import ApplyMatchOutcome
from payment_module.application.config import PaymentModuleConfig, retry_delay
from payment_module.domain.enums import (
    Direction,
    FirstSource,
    IdentityKind,
    InboxStatus,
    LinkMethod,
    LinkStatus,
    MatchState,
    ObservationSource,
    ReceiptTimeSource,
    SettlementOrigin,
)
from payment_module.domain.errors import DomainError, IllegalTransition, PolicyViolation
from payment_module.domain.intent import IntentView
from payment_module.domain.matching.invariant_guard import ConnectionView
from payment_module.domain.matching.match_transaction import MatchContext, MatchTransaction
from payment_module.domain.reference import tokens_from
from payment_module.domain.transaction import TransactionView
from payment_module.ports.clock import Clock
from payment_module.ports.metrics import MetricsSink
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

NORMALIZE_FAILED = "normalize_failed"
POLICY_VIOLATION = "policy_violation"
OBSERVATION_CONFLICT = "observation_conflict"


class _ObservationConflict(Exception):
    """The observation's source id is already stored, but for a different fact."""


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


async def _link_or_create_fact(
    uow: UnitOfWork,
    connection: ProviderConnection,
    obs: NormalizedObservation,
    receiver: ReceivingAccountView | None,
) -> tuple[TransactionView, bool]:
    """Insert the canonical fact of a webhook observation, or lock the existing one.

    Returns ``(fact, created)``; the fact row is locked for the rest of the transaction.
    Reconciliation extends this step: a webhook that arrives after an API sighting of the
    same money will be linked to that canonical fact instead of creating a second one.
    """
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
    )
    tx, created = await uow.transactions.insert_or_get_by_dedup_key(new)
    if created:
        return tx, True
    locked = await uow.transactions.get_for_update(tx.tenant_id, tx.environment, tx.id)
    assert locked is not None
    return locked, False


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
        """Process a row this worker claimed; safe to call after the lease was reclaimed."""
        try:
            async with self._uow_factory() as uow:
                if not await uow.inbox.lock_claim(claimed.id, claimed.lease_generation):
                    return self._lease_lost(claimed)
                status, error_code = await self._handle(uow, claimed)
                finalized = await uow.inbox.finalize(
                    claimed.id, claimed.lease_generation, status, last_error_code=error_code
                )
                if not finalized:
                    await uow.rollback()
                    return self._lease_lost(claimed)
                await uow.commit()
            return ProcessOneResult(ProcessStatus.PROCESSED, status.value)
        except _ObservationConflict:
            logger.error("payment_inbox_observation_conflict", extra={"inbox_id": str(claimed.id)})
            return await self._record_failure(claimed, InboxStatus.FAILED, OBSERVATION_CONFLICT)
        except (PolicyViolation, IllegalTransition) as exc:
            logger.warning(
                "payment_inbox_invariant_violation",
                extra={"inbox_id": str(claimed.id), "error": type(exc).__name__},
            )
            return await self._record_failure(claimed, InboxStatus.FAILED, POLICY_VIOLATION)
        except Exception as exc:
            error = type(exc).__name__
            if claimed.attempts >= self._config.inbox_max_attempts:
                logger.error(
                    "payment_inbox_failed", extra={"inbox_id": str(claimed.id), "error": error}
                )
                return await self._record_failure(claimed, InboxStatus.FAILED, error)
            logger.warning(
                "payment_inbox_retry",
                extra={
                    "inbox_id": str(claimed.id),
                    "error": error,
                    "attempts": claimed.attempts,
                },
            )
            next_attempt_at = self._clock.now() + retry_delay(claimed.attempts)
            return await self._record_failure(
                claimed, InboxStatus.RETRY_WAIT, error, next_attempt_at
            )

    async def _handle(
        self, uow: UnitOfWork, claimed: ClaimedInbox
    ) -> tuple[InboxStatus, str | None]:
        connection = await uow.connections.get(claimed.tenant_id, claimed.connection_id)
        if connection is None:
            raise LookupError(f"connection of inbox row {claimed.id} not found")
        provider = provider_for(self._providers, connection.provider)
        delivery = VerifiedDelivery(claimed.raw_body, claimed.headers, claimed.received_at)
        try:
            obs = provider.normalize(delivery)
        except (DomainError, ValueError):
            self._metrics.increment(
                "inbox_quarantined_total",
                {"connection_id": str(connection.id), "reason": NORMALIZE_FAILED},
            )
            logger.warning("payment_inbox_quarantined", extra={"inbox_id": str(claimed.id)})
            return InboxStatus.QUARANTINED, NORMALIZE_FAILED

        receiver = await self._receiver(uow, connection, obs)
        tx, created = await _link_or_create_fact(uow, connection, obs, receiver)
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
            link_method=LinkMethod.SAME_SOURCE_ID,
            link_status=LinkStatus.LINKED,
            purge_after=self._config.purge_after(claimed.received_at),
        )
        if observation_id is None and created:
            # Same source id, different account or payload: never merge money silently.
            raise _ObservationConflict
        if observation_id is None or tx.match_state != MatchState.RECORDED:
            # A replay of money already seen: the outcome is never applied twice.
            return InboxStatus.PROCESSED, None

        await self._decide(uow, connection, obs, tx, claimed.received_at)
        return InboxStatus.PROCESSED, None

    @staticmethod
    async def _receiver(
        uow: UnitOfWork, connection: ProviderConnection, obs: NormalizedObservation
    ) -> ReceivingAccountView | None:
        """The registered account the payload names, if it belongs to the connection's tenant.

        An account of another merchant of the same tenant is still recorded, so the guard
        reports the fact as unbound; an account of another tenant is never linked.
        """
        account = await uow.receiving_accounts.find_by_fingerprint(
            connection.environment, obs.reported_account_key
        )
        if account is None or account.tenant_id != connection.tenant_id:
            return None
        return account

    async def _decide(
        self,
        uow: UnitOfWork,
        connection: ProviderConnection,
        obs: NormalizedObservation,
        tx: TransactionView,
        received_at: datetime,
    ) -> None:
        bound = frozenset(await uow.connection_bindings.account_ids(connection.id))
        tokens = tuple(tokens_from(obs.code, obs.content))
        candidates: dict[str, IntentView] = {}
        if tx.direction == Direction.IN and tx.receiving_account_id in bound and tokens:
            for intent in await uow.intents.find_by_references_for_update(tokens):
                candidates[intent.payment_reference] = intent
        outcome = self._match.decide(
            MatchContext(
                tx=tx,
                connection=ConnectionView(
                    connection.id,
                    connection.tenant_id,
                    connection.merchant_id,
                    connection.environment,
                ),
                bound_account_ids=bound,
                tokens=tokens,
                candidates=candidates,
                effective_received_at=received_at,
                time_source=ReceiptTimeSource.WEBHOOK_RECEIVED,
            )
        )
        await self._apply.apply(
            uow,
            tx=tx,
            outcome=outcome,
            intents={intent.id: intent for intent in candidates.values()},
            origin=SettlementOrigin.AUTO,
        )

    async def _record_failure(
        self,
        claimed: ClaimedInbox,
        status: InboxStatus,
        error_code: str,
        next_attempt_at: datetime | None = None,
    ) -> ProcessOneResult:
        try:
            async with self._uow_factory() as uow:
                recorded = await uow.inbox.finalize(
                    claimed.id,
                    claimed.lease_generation,
                    status,
                    next_attempt_at=next_attempt_at,
                    last_error_code=error_code[:64],
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
        return ProcessOneResult(ProcessStatus.PROCESSED, status.value)

    @staticmethod
    def _lease_lost(claimed: ClaimedInbox) -> ProcessOneResult:
        logger.warning(
            "payment_inbox_lease_lost",
            extra={"inbox_id": str(claimed.id), "lease_generation": claimed.lease_generation},
        )
        return ProcessOneResult(ProcessStatus.SKIPPED, "lease_lost")
