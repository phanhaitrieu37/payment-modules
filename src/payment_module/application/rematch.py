"""Run matching again for facts in review after the operator fixed what blocked them.

``RematchUnbound`` handles ``RECEIVER_UNBOUND`` facts of one connection once an account was
bound to it: the fact gets its receiver, the old case is resolved ``bind_receiver`` and the
new outcome is applied (a settlement, or a case with another reason). ``RematchReviews``
retries open cases of other reasons, for example ``NO_REFERENCE`` after the host imported
intents for old codes: a settlement resolves the case ``attach_to_intent``; another reason
updates the open case in place; the same reason leaves it untouched.

Each fact is rematched in its own transaction with the processing lock order: fact, then
candidate intents (by id), then the review case. The reference and the receipt time come
from the fact's observations; the fact itself stores neither memo nor code.
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from payment_module.application.apply_match_outcome import (
    TRANSACTION_AGGREGATE,
    ApplyMatchOutcome,
    outbox_view,
)
from payment_module.application.match_context import load_match_context, resolve_receiver
from payment_module.application.readiness import require_actor
from payment_module.domain.enums import (
    MatchState,
    ObservationSource,
    ReceiptTimeSource,
    ReviewCaseStatus,
    ReviewReason,
    ReviewResolution,
    SettlementOrigin,
)
from payment_module.domain.events import ReviewResolved
from payment_module.domain.intent import IntentView
from payment_module.domain.matching.match_transaction import MatchContext, MatchTransaction
from payment_module.domain.reference import tokens_from
from payment_module.domain.review import MatchOutcome, Review, Settle
from payment_module.domain.transaction import TransactionView
from payment_module.ports.clock import Clock
from payment_module.ports.handlers import TransactionOutcomeView
from payment_module.ports.resolvers import ProviderConnection
from payment_module.ports.unit_of_work import (
    ObservationView,
    ReviewCaseView,
    UnitOfWork,
    UnitOfWorkFactory,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Sighting:
    """What a rematch reads from the fact's observations: the connection it arrived
    through, the reference text, and the receipt time for the late check."""

    connection: ProviderConnection
    code: str | None
    memo: str | None
    received_at: datetime
    time_source: ReceiptTimeSource


@dataclass(frozen=True, slots=True)
class RematchResult:
    """``changed`` is ``False`` when the fact still needs review for the same reason."""

    review_case_id: UUID
    transaction_id: UUID
    changed: bool
    match_state: MatchState | None = None
    review_reason: ReviewReason | None = None


async def sighting(uow: UnitOfWork, tx: TransactionView) -> Sighting:
    """The first webhook observation of ``tx`` (its receipt counts as webhook receipt), or
    else its first observation at observation time."""
    observations = await uow.observations.list_for_transaction(tx.id)
    if not observations:
        raise LookupError(f"transaction {tx.id} has no observation")
    webhook = next((o for o in observations if o.source == ObservationSource.WEBHOOK), None)
    first: ObservationView = webhook or observations[0]
    connection = await uow.connections.get(tx.tenant_id, first.connection_id)
    if connection is None:
        raise LookupError(f"connection of transaction {tx.id} not found")
    return Sighting(
        connection=connection,
        code=first.code,
        memo=first.memo,
        received_at=first.observed_at,
        time_source=(ReceiptTimeSource.WEBHOOK_RECEIVED if webhook else ReceiptTimeSource.OBSERVED),
    )


async def match_context_of(
    uow: UnitOfWork, tx: TransactionView, seen: Sighting
) -> tuple[MatchContext, dict[UUID, IntentView]]:
    return await load_match_context(
        uow,
        tx=tx,
        connection=seen.connection,
        tokens=tokens_from(seen.code, seen.memo),
        effective_received_at=seen.received_at,
        time_source=seen.time_source,
    )


async def record_resolved(
    uow: UnitOfWork,
    *,
    case: ReviewCaseView,
    resolution: ReviewResolution,
    actor: str,
    now: datetime,
    note: str | None = None,
    resolution_ref: str | None = None,
    details: Mapping[str, object] | None = None,
) -> None:
    """Resolve the open case; the caller emits :class:`ReviewResolved` once it knows the
    settlement, if any."""
    resolved = await uow.review_cases.resolve(
        case.id,
        resolution=resolution,
        resolved_by=actor,
        resolution_ref=resolution_ref,
        resolution_note=note,
        resolved_at=now,
        details=details,
    )
    if not resolved:
        raise LookupError(f"review case {case.id} is no longer open")


async def emit_review_resolved(
    uow: UnitOfWork,
    *,
    case: ReviewCaseView,
    resolution: ReviewResolution,
    actor: str,
    settlement_id: UUID | None,
    now: datetime,
) -> None:
    event = ReviewResolved(
        event_id=uuid.uuid4(),
        tenant_id=case.tenant_id,
        environment=case.environment,
        review_case_id=case.id,
        transaction_id=case.transaction_id,
        resolution=resolution,
        resolved_by=actor,
        settlement_id=settlement_id,
    )
    await uow.outbox.add(outbox_view(event, now), TRANSACTION_AGGREGATE, case.transaction_id, now)
    logger.info(
        "payment_review_resolved",
        extra={
            "review_case_id": str(case.id),
            "resolution": resolution.value,
            "actor": actor,
        },
    )


class _Rematch:
    reasons: frozenset[ReviewReason]

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        match: MatchTransaction,
        apply_outcome: ApplyMatchOutcome,
        clock: Clock,
    ) -> None:
        self._uow_factory = uow_factory
        self._match = match
        self._apply = apply_outcome
        self._clock = clock

    async def _run(
        self,
        tenant_id: str,
        actor: str,
        reasons: Collection[ReviewReason],
        *,
        connection_id: UUID | None = None,
        opened_after: datetime | None = None,
    ) -> list[RematchResult]:
        require_actor(actor)
        async with self._uow_factory() as uow:
            cases = await uow.review_cases.list_open(
                tenant_id, reasons, connection_id=connection_id, opened_after=opened_after
            )
        return [
            await self.rematch_case(tenant_id, case.id, actor, reasons=frozenset(reasons))
            for case in cases
        ]

    async def rematch_case(
        self,
        tenant_id: str,
        case_id: UUID,
        actor: str,
        *,
        reasons: frozenset[ReviewReason] | None = None,
        note: str | None = None,
        resolution_ref: str | None = None,
    ) -> RematchResult:
        """Rematch the fact of one open case whose reason is in ``reasons``."""
        allowed = self.reasons if reasons is None else reasons
        async with self._uow_factory() as uow:
            case = await uow.review_cases.get(tenant_id, case_id)
            if case is None:
                raise LookupError(f"review case {case_id} not found")
            tx = await uow.transactions.get_for_update(
                tenant_id, case.environment, case.transaction_id
            )
            assert tx is not None
            seen = await sighting(uow, tx)
            tx = await self._prepare(uow, tx, seen)
            ctx, intents = await match_context_of(uow, tx, seen)
            locked = await uow.review_cases.get_for_update(case.id)
            if locked is None or locked.status != ReviewCaseStatus.OPEN:
                return RematchResult(case.id, tx.id, changed=False)
            if locked.reason not in allowed:
                return RematchResult(case.id, tx.id, changed=False, review_reason=locked.reason)
            outcome = self._match.decide(ctx)
            if isinstance(outcome, Review) and outcome.reason == locked.reason:
                return RematchResult(case.id, tx.id, changed=False, review_reason=locked.reason)
            view = await self._apply_outcome(
                uow, locked, tx, outcome, intents, actor, note, resolution_ref
            )
            await uow.commit()
        self._apply.after_commit(view, outcome)
        return RematchResult(case.id, tx.id, True, view.match_state, view.review_reason)

    async def _prepare(
        self, uow: UnitOfWork, tx: TransactionView, seen: Sighting
    ) -> TransactionView:
        return tx

    async def _apply_outcome(
        self,
        uow: UnitOfWork,
        case: ReviewCaseView,
        tx: TransactionView,
        outcome: MatchOutcome,
        intents: Mapping[UUID, IntentView],
        actor: str,
        note: str | None,
        resolution_ref: str | None,
    ) -> TransactionOutcomeView:
        raise NotImplementedError

    async def _resolve_and_apply(
        self,
        uow: UnitOfWork,
        case: ReviewCaseView,
        tx: TransactionView,
        outcome: MatchOutcome,
        intents: Mapping[UUID, IntentView],
        *,
        resolution: ReviewResolution,
        actor: str,
        note: str | None,
        resolution_ref: str | None,
    ) -> TransactionOutcomeView:
        """Resolve the case first, so a new reason opens a new case instead of rewriting
        this one, then apply the outcome and announce the resolution."""
        now = self._clock.now()
        await record_resolved(
            uow,
            case=case,
            resolution=resolution,
            actor=actor,
            now=now,
            note=note,
            resolution_ref=resolution_ref,
        )
        view = await self._apply.apply(
            uow, tx=tx, outcome=outcome, intents=intents, origin=SettlementOrigin.AUTO
        )
        await emit_review_resolved(
            uow,
            case=case,
            resolution=resolution,
            actor=actor,
            settlement_id=view.settlement_id,
            now=now,
        )
        return view


class RematchUnbound(_Rematch):
    reasons = frozenset({ReviewReason.RECEIVER_UNBOUND})

    async def execute(self, tenant_id: str, connection_id: UUID, actor: str) -> list[RematchResult]:
        """Every open ``RECEIVER_UNBOUND`` case of facts seen through ``connection_id``."""
        return await self._run(tenant_id, actor, self.reasons, connection_id=connection_id)

    async def _prepare(
        self, uow: UnitOfWork, tx: TransactionView, seen: Sighting
    ) -> TransactionView:
        if tx.receiving_account_id is not None:
            return tx
        receiver = await resolve_receiver(uow, seen.connection, tx.provider_account_key)
        if receiver is None:
            return tx
        if not await uow.transactions.set_receiver(tx.id, receiver.merchant_id, receiver.id):
            return tx
        return dataclasses.replace(
            tx, receiving_account_id=receiver.id, merchant_id=receiver.merchant_id
        )

    async def _apply_outcome(
        self,
        uow: UnitOfWork,
        case: ReviewCaseView,
        tx: TransactionView,
        outcome: MatchOutcome,
        intents: Mapping[UUID, IntentView],
        actor: str,
        note: str | None,
        resolution_ref: str | None,
    ) -> TransactionOutcomeView:
        # The binding answered this case, whatever matching says next.
        return await self._resolve_and_apply(
            uow,
            case,
            tx,
            outcome,
            intents,
            resolution=ReviewResolution.BIND_RECEIVER,
            actor=actor,
            note=note,
            resolution_ref=resolution_ref,
        )


class RematchReviews(_Rematch):
    reasons = frozenset({ReviewReason.NO_REFERENCE})

    async def execute(
        self,
        tenant_id: str,
        actor: str,
        reasons: Collection[ReviewReason] = frozenset({ReviewReason.NO_REFERENCE}),
        opened_after: datetime | None = None,
    ) -> list[RematchResult]:
        """Open cases of ``reasons`` opened at or after ``opened_after``.

        ``RECEIVER_UNBOUND`` is not accepted here: those need a binding and
        :class:`RematchUnbound`.
        """
        reasons = frozenset(ReviewReason(reason) for reason in reasons)
        if ReviewReason.RECEIVER_UNBOUND in reasons:
            raise ValueError("rematch RECEIVER_UNBOUND cases with RematchUnbound")
        return await self._run(tenant_id, actor, reasons, opened_after=opened_after)

    async def _apply_outcome(
        self,
        uow: UnitOfWork,
        case: ReviewCaseView,
        tx: TransactionView,
        outcome: MatchOutcome,
        intents: Mapping[UUID, IntentView],
        actor: str,
        note: str | None,
        resolution_ref: str | None,
    ) -> TransactionOutcomeView:
        if not isinstance(outcome, Settle):
            # Another reason: the open case is updated in place with the new reason.
            return await self._apply.apply(
                uow, tx=tx, outcome=outcome, intents=intents, origin=SettlementOrigin.AUTO
            )
        return await self._resolve_and_apply(
            uow,
            case,
            tx,
            outcome,
            intents,
            resolution=ReviewResolution.ATTACH_TO_INTENT,
            actor=actor,
            note=note,
            resolution_ref=resolution_ref,
        )
