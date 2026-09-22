"""Operator resolution of a review case: the only place an operator moves money state.

No resolution settles money whose amount differs from the intent amount, and there is no
parameter to accept a difference. Money in review for a wrong amount can only be closed as
external, marked a duplicate of another fact, or attached to **another** intent of exactly
its amount. Every settle goes through :class:`MatchTransaction` (guard, scope, eligibility,
policy, exact-amount post-check) with an audited :class:`OperatorOverride`, then through the
database's own CHECK and composite foreign keys.

Resolutions allowed per reason:

- ``attach_to_intent(intent_id)``: every reason but ``RECEIVER_UNBOUND``; the intent must be
  in the fact's tenant and environment, and matching must settle it.
- ``accept_late``: only ``LATE``, for the case's candidate intent; lateness is waived, the
  amount is not.
- ``mark_external`` and ``mark_duplicate_of(transaction_id)``: every reason; both need a
  ``note`` or ``resolution_ref`` as provenance.
- ``bind_receiver(receiving_account_id)``: only ``RECEIVER_UNBOUND``; binds the account to
  the connection the fact arrived through, then rematches that connection's unbound facts.

Lock order is the processing order: fact, then intent, then the review case.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from payment_module.application.apply_match_outcome import (
    ApplyMatchOutcome,
    HandlerFailed,
    outcome_view,
)
from payment_module.application.match_context import connection_view
from payment_module.application.onboarding import BindConnectionAccount
from payment_module.application.readiness import require_actor
from payment_module.application.rematch import (
    RematchUnbound,
    emit_review_resolved,
    record_resolved,
    sighting,
)
from payment_module.domain.enums import (
    Environment,
    MatchState,
    ReviewCaseStatus,
    ReviewReason,
    ReviewResolution,
    SettlementOrigin,
)
from payment_module.domain.errors import (
    IllegalTransition,
    ResolutionNotAllowed,
    ReviewAlreadyResolved,
    ReviewCaseNotFound,
)
from payment_module.domain.intent import IntentView
from payment_module.domain.matching.match_transaction import (
    MatchContext,
    MatchTransaction,
    OperatorOverride,
)
from payment_module.domain.reference import tokens_from
from payment_module.domain.review import Review, Settle
from payment_module.domain.transaction import TransactionView, transition_match_state
from payment_module.ports.clock import Clock
from payment_module.ports.handlers import OutcomeObserver, TransactionOutcomeView
from payment_module.ports.unit_of_work import ReviewCaseView, UnitOfWork, UnitOfWorkFactory

logger = logging.getLogger(__name__)

RESOLUTION_NOT_ALLOWED = "RESOLUTION_NOT_ALLOWED"
INVALID_RESOLUTION_REF = "INVALID_RESOLUTION_REF"
INTENT_REQUIRED = "INTENT_REQUIRED"
INTENT_NOT_FOUND = "INTENT_NOT_FOUND"
PROVENANCE_REQUIRED = "PROVENANCE_REQUIRED"
DUPLICATE_TARGET_REQUIRED = "DUPLICATE_TARGET_REQUIRED"
DUPLICATE_TARGET_NOT_FOUND = "DUPLICATE_TARGET_NOT_FOUND"
DUPLICATE_TARGET_MISMATCH = "DUPLICATE_TARGET_MISMATCH"
ACCOUNT_REQUIRED = "ACCOUNT_REQUIRED"
ACCOUNT_NOT_FOUND = "ACCOUNT_NOT_FOUND"
ACCOUNT_KEY_MISMATCH = "ACCOUNT_KEY_MISMATCH"
NOT_APPLICABLE = "NOT_APPLICABLE"

_RESOLUTION_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
_NOT_DUPLICATE_TARGETS = frozenset({MatchState.DUPLICATE_OF, MatchState.NOT_APPLICABLE})


def resolution_allowed(reason: ReviewReason, resolution: ReviewResolution) -> bool:
    match ReviewResolution(resolution):
        case ReviewResolution.ATTACH_TO_INTENT:
            return reason != ReviewReason.RECEIVER_UNBOUND
        case ReviewResolution.ACCEPT_LATE:
            return reason == ReviewReason.LATE
        case ReviewResolution.BIND_RECEIVER:
            return reason == ReviewReason.RECEIVER_UNBOUND
        case ReviewResolution.MARK_EXTERNAL | ReviewResolution.MARK_DUPLICATE_OF:
            return True


@dataclass(frozen=True, slots=True)
class ReviewView:
    review_case_id: UUID
    tenant_id: str
    environment: Environment
    transaction_id: UUID
    reason: ReviewReason
    status: ReviewCaseStatus
    resolution: ReviewResolution | None
    resolution_ref: str | None
    resolved_by: str | None
    resolution_note: str | None
    resolved_at: datetime | None
    candidate_intent_id: UUID | None
    match_state: MatchState
    settlement_id: UUID | None = None


class ResolveReview:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        match: MatchTransaction,
        apply_outcome: ApplyMatchOutcome,
        outcome_observer: OutcomeObserver,
        clock: Clock,
        bind_account: BindConnectionAccount,
        rematch_unbound: RematchUnbound,
    ) -> None:
        self._uow_factory = uow_factory
        self._match = match
        self._apply = apply_outcome
        self._observer = outcome_observer
        self._clock = clock
        self._bind = bind_account
        self._rematch_unbound = rematch_unbound

    async def execute(
        self,
        tenant_id: str,
        review_case_id: UUID,
        resolution: ReviewResolution,
        actor: str,
        note: str | None = None,
        resolution_ref: str | None = None,
        *,
        intent_id: UUID | None = None,
        duplicate_of_transaction_id: UUID | None = None,
        receiving_account_id: UUID | None = None,
    ) -> ReviewView:
        """Resolve an open case; see the module docstring for what each resolution needs.

        ``resolution_ref`` is a structured host code of at most 64 characters (for example
        ``refunded``), stored with the case. Raises :class:`ReviewCaseNotFound`,
        :class:`ReviewAlreadyResolved` or :class:`ResolutionNotAllowed` (``code`` is the
        reason, for example ``AMOUNT_MISMATCH`` when matching would not settle).
        """
        require_actor(actor)
        resolution = ReviewResolution(resolution)
        if resolution_ref is not None and not _RESOLUTION_REF.fullmatch(resolution_ref):
            raise ResolutionNotAllowed(INVALID_RESOLUTION_REF)
        if resolution == ReviewResolution.BIND_RECEIVER:
            return await self._bind_receiver(
                tenant_id, review_case_id, actor, note, resolution_ref, receiving_account_id
            )
        async with self._uow_factory() as uow:
            case = await self._case(uow, tenant_id, review_case_id)
            if not resolution_allowed(case.reason, resolution):
                raise ResolutionNotAllowed(RESOLUTION_NOT_ALLOWED)
            tx = await uow.transactions.get_for_update(
                tenant_id, case.environment, case.transaction_id
            )
            assert tx is not None
            intent = None
            if resolution in (ReviewResolution.ATTACH_TO_INTENT, ReviewResolution.ACCEPT_LATE):
                target = (
                    intent_id
                    if resolution == ReviewResolution.ATTACH_TO_INTENT
                    else case.candidate_intent_id
                )
                if target is None:
                    raise ResolutionNotAllowed(INTENT_REQUIRED)
                intent = await uow.intents.get_for_update(tenant_id, target)
            case = await self._lock_open(uow, case, resolution)
            if tx.match_state != MatchState.IN_REVIEW:
                raise IllegalTransition("provider_transaction", tx.match_state.value, "resolved")
            now = self._clock.now()
            settled: Settle | None = None
            if resolution == ReviewResolution.MARK_EXTERNAL:
                view = await self._mark_external(uow, case, tx, actor, note, resolution_ref, now)
            elif resolution == ReviewResolution.MARK_DUPLICATE_OF:
                view = await self._mark_duplicate(
                    uow, case, tx, actor, note, resolution_ref, now, duplicate_of_transaction_id
                )
            else:
                view, settled = await self._settle(
                    uow, case, tx, intent, resolution, actor, note, resolution_ref, now
                )
            await uow.commit()
        if settled is not None:
            self._apply.after_commit(view, settled)
        logger.info(
            "payment_review_case_resolved",
            extra={
                "review_case_id": str(case.id),
                "resolution": resolution.value,
                "actor": actor,
                "match_state": view.match_state.value,
            },
        )
        return _review_view(case, view, resolution, actor, note, resolution_ref, now)

    @staticmethod
    async def _case(uow: UnitOfWork, tenant_id: str, case_id: UUID) -> ReviewCaseView:
        case = await uow.review_cases.get(tenant_id, case_id)
        if case is None:
            raise ReviewCaseNotFound(f"review case {case_id} not found")
        return case

    @staticmethod
    async def _lock_open(
        uow: UnitOfWork, case: ReviewCaseView, resolution: ReviewResolution
    ) -> ReviewCaseView:
        locked = await uow.review_cases.get_for_update(case.id)
        if locked is None or locked.status != ReviewCaseStatus.OPEN:
            raise ReviewAlreadyResolved(f"review case {case.id} is not open")
        if not resolution_allowed(locked.reason, resolution):
            raise ResolutionNotAllowed(RESOLUTION_NOT_ALLOWED)
        return locked

    async def _settle(
        self,
        uow: UnitOfWork,
        case: ReviewCaseView,
        tx: TransactionView,
        intent: IntentView | None,
        resolution: ReviewResolution,
        actor: str,
        note: str | None,
        resolution_ref: str | None,
        now: datetime,
    ) -> tuple[TransactionOutcomeView, Settle]:
        if intent is None or intent.environment != tx.environment:
            raise ResolutionNotAllowed(INTENT_NOT_FOUND)
        seen = await sighting(uow, tx)
        bound = frozenset(await uow.connection_bindings.account_ids(seen.connection.id))
        ctx = MatchContext(
            tx=tx,
            connection=connection_view(seen.connection),
            bound_account_ids=bound,
            tokens=tuple(tokens_from(seen.code, seen.memo)),
            candidates={intent.payment_reference: intent},
            effective_received_at=seen.received_at,
            time_source=seen.time_source,
        )
        override = OperatorOverride(
            forced_intent_id=intent.id,
            accept_late=resolution == ReviewResolution.ACCEPT_LATE,
        )
        outcome = self._match.decide(ctx, override)
        if not isinstance(outcome, Settle):
            code = outcome.reason.value if isinstance(outcome, Review) else NOT_APPLICABLE
            raise ResolutionNotAllowed(code)
        view = await self._apply.apply(
            uow,
            tx=tx,
            outcome=outcome,
            intents={intent.id: intent},
            origin=SettlementOrigin.OPERATOR_REVIEW,
            review_case_id=case.id,
            resolved_by=actor,
        )
        await record_resolved(
            uow,
            case=case,
            resolution=resolution,
            actor=actor,
            now=now,
            note=note,
            resolution_ref=resolution_ref,
        )
        await emit_review_resolved(
            uow,
            case=case,
            resolution=resolution,
            actor=actor,
            settlement_id=view.settlement_id,
            now=now,
        )
        return view, outcome

    async def _mark_external(
        self,
        uow: UnitOfWork,
        case: ReviewCaseView,
        tx: TransactionView,
        actor: str,
        note: str | None,
        resolution_ref: str | None,
        now: datetime,
    ) -> TransactionOutcomeView:
        _require_provenance(note, resolution_ref)
        target = transition_match_state(tx.match_state, MatchState.CLOSED_EXTERNAL)
        if not await uow.transactions.set_match_state(tx.id, tx.match_state, target):
            raise IllegalTransition("provider_transaction", tx.match_state.value, target.value)
        return await self._close(
            uow, case, tx, target, ReviewResolution.MARK_EXTERNAL, actor, note, resolution_ref, now
        )

    async def _mark_duplicate(
        self,
        uow: UnitOfWork,
        case: ReviewCaseView,
        tx: TransactionView,
        actor: str,
        note: str | None,
        resolution_ref: str | None,
        now: datetime,
        duplicate_of: UUID | None,
    ) -> TransactionOutcomeView:
        """The original must be the same money seen twice: same tenant, environment and
        provider account key, same amount and direction, another fact that is itself not a
        duplicate or outgoing noise."""
        _require_provenance(note, resolution_ref)
        if duplicate_of is None:
            raise ResolutionNotAllowed(DUPLICATE_TARGET_REQUIRED)
        original = await uow.transactions.get(tx.tenant_id, tx.environment, duplicate_of)
        if original is None:
            raise ResolutionNotAllowed(DUPLICATE_TARGET_NOT_FOUND)
        if (
            original.id == tx.id
            or original.provider_account_key != tx.provider_account_key
            or original.amount != tx.amount
            or original.direction != tx.direction
            or original.match_state in _NOT_DUPLICATE_TARGETS
        ):
            raise ResolutionNotAllowed(DUPLICATE_TARGET_MISMATCH)
        target = transition_match_state(tx.match_state, MatchState.DUPLICATE_OF)
        if not await uow.transactions.mark_duplicate_of(tx.id, original.id):
            raise IllegalTransition("provider_transaction", tx.match_state.value, target.value)
        details = dict(case.details) | {"duplicate_of": str(original.id)}
        return await self._close(
            uow,
            case,
            tx,
            target,
            ReviewResolution.MARK_DUPLICATE_OF,
            actor,
            note,
            resolution_ref,
            now,
            details=details,
        )

    async def _close(
        self,
        uow: UnitOfWork,
        case: ReviewCaseView,
        tx: TransactionView,
        state: MatchState,
        resolution: ReviewResolution,
        actor: str,
        note: str | None,
        resolution_ref: str | None,
        now: datetime,
        *,
        details: dict[str, object] | None = None,
    ) -> TransactionOutcomeView:
        await record_resolved(
            uow,
            case=case,
            resolution=resolution,
            actor=actor,
            now=now,
            note=note,
            resolution_ref=resolution_ref,
            details=details,
        )
        await emit_review_resolved(
            uow, case=case, resolution=resolution, actor=actor, settlement_id=None, now=now
        )
        view = outcome_view(tx, state, review_case_id=case.id)
        try:
            await self._observer.on_outcome(uow, view)
        except Exception as exc:
            raise HandlerFailed("outcome observer failed") from exc
        return view

    async def _bind_receiver(
        self,
        tenant_id: str,
        case_id: UUID,
        actor: str,
        note: str | None,
        resolution_ref: str | None,
        receiving_account_id: UUID | None,
    ) -> ReviewView:
        """Bind, resolve this case with the operator's note, then rematch the other
        unbound facts of the same connection."""
        if receiving_account_id is None:
            raise ResolutionNotAllowed(ACCOUNT_REQUIRED)
        async with self._uow_factory() as uow:
            case = await self._case(uow, tenant_id, case_id)
            if case.status != ReviewCaseStatus.OPEN:
                raise ReviewAlreadyResolved(f"review case {case.id} is not open")
            if not resolution_allowed(case.reason, ReviewResolution.BIND_RECEIVER):
                raise ResolutionNotAllowed(RESOLUTION_NOT_ALLOWED)
            tx = await uow.transactions.get(tenant_id, case.environment, case.transaction_id)
            assert tx is not None
            account = await uow.receiving_accounts.get(tenant_id, receiving_account_id)
            if account is None:
                raise ResolutionNotAllowed(ACCOUNT_NOT_FOUND)
            fingerprints = await uow.receiving_accounts.fingerprints([account.id])
            if fingerprints.get(account.id) != tx.provider_account_key:
                # Binding an account the money did not go to would not unblock this fact.
                raise ResolutionNotAllowed(ACCOUNT_KEY_MISMATCH)
            connection_id = (await sighting(uow, tx)).connection.id
        await self._bind.execute(tenant_id, connection_id, receiving_account_id, actor)
        result = await self._rematch_unbound.rematch_case(
            tenant_id, case_id, actor, note=note, resolution_ref=resolution_ref
        )
        if not result.changed:
            raise ResolutionNotAllowed(ReviewReason.RECEIVER_UNBOUND.value)
        await self._rematch_unbound.execute(tenant_id, connection_id, actor)
        async with self._uow_factory() as uow:
            resolved = await self._case(uow, tenant_id, case_id)
            fact = await uow.transactions.get(tenant_id, case.environment, case.transaction_id)
        assert fact is not None
        return ReviewView(
            review_case_id=resolved.id,
            tenant_id=resolved.tenant_id,
            environment=resolved.environment,
            transaction_id=resolved.transaction_id,
            reason=resolved.reason,
            status=resolved.status,
            resolution=resolved.resolution,
            resolution_ref=resolved.resolution_ref,
            resolved_by=resolved.resolved_by,
            resolution_note=resolved.resolution_note,
            resolved_at=resolved.resolved_at,
            candidate_intent_id=resolved.candidate_intent_id,
            match_state=fact.match_state,
        )


def _require_provenance(note: str | None, resolution_ref: str | None) -> None:
    if not (note and note.strip()) and not resolution_ref:
        raise ResolutionNotAllowed(PROVENANCE_REQUIRED)


def _review_view(
    case: ReviewCaseView,
    outcome: TransactionOutcomeView,
    resolution: ReviewResolution,
    actor: str,
    note: str | None,
    resolution_ref: str | None,
    resolved_at: datetime,
) -> ReviewView:
    return ReviewView(
        review_case_id=case.id,
        tenant_id=case.tenant_id,
        environment=case.environment,
        transaction_id=case.transaction_id,
        reason=case.reason,
        status=ReviewCaseStatus.RESOLVED,
        resolution=resolution,
        resolution_ref=resolution_ref,
        resolved_by=actor,
        resolution_note=note,
        resolved_at=resolved_at,
        candidate_intent_id=case.candidate_intent_id,
        match_state=outcome.match_state,
        settlement_id=outcome.settlement_id,
    )
