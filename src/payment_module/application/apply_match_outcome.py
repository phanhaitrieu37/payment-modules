"""Write one matching outcome: settlement, review or not-applicable, in the caller's UoW.

Shared by inbox processing, reconciliation and operator review. Everything here runs in the
caller's transaction: the settlement, the intent and fact state changes, the host handler,
the outbox event and the observer commit together or not at all. Any exception rolls the
whole outcome back.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

from payment_module.domain.enums import (
    Direction,
    IntentStatus,
    MatchState,
    ReviewReason,
    SettlementOrigin,
)
from payment_module.domain.errors import IllegalTransition, PolicyViolation
from payment_module.domain.events import PaymentNeedsReview, PaymentSettled, ReviewResolved
from payment_module.domain.intent import IntentView, transition_intent
from payment_module.domain.matching.match_transaction import ensure_settle_allowed
from payment_module.domain.review import MatchOutcome, NotApplicable, Review, Settle
from payment_module.domain.transaction import TransactionView, transition_match_state
from payment_module.ports.clock import Clock
from payment_module.ports.handlers import (
    OutcomeObserver,
    SettlementHandler,
    SettlementView,
    TransactionOutcomeView,
)
from payment_module.ports.metrics import MetricsSink, increment_safely
from payment_module.ports.publisher import OutboxEventView
from payment_module.ports.unit_of_work import UnitOfWork

logger = logging.getLogger(__name__)

INTENT_AGGREGATE = "payment_intent"
TRANSACTION_AGGREGATE = "provider_transaction"


class HandlerFailed(Exception):
    """A host hook (settlement handler or outcome observer) raised; ``__cause__`` has it."""


def outbox_view(
    event: PaymentSettled | PaymentNeedsReview | ReviewResolved, occurred_at: datetime
) -> OutboxEventView:
    return OutboxEventView(
        event_id=event.event_id,
        event_type=event.event_type,
        schema_version=event.schema_version,
        trusted_scope=event.trusted_scope,
        payload=event.to_payload(),
        occurred_at=occurred_at,
    )


class ApplyMatchOutcome:
    def __init__(
        self,
        settlement_handler: SettlementHandler,
        outcome_observer: OutcomeObserver,
        metrics: MetricsSink,
        clock: Clock,
    ) -> None:
        self._handler = settlement_handler
        self._observer = outcome_observer
        self._metrics = metrics
        self._clock = clock

    async def apply(
        self,
        uow: UnitOfWork,
        *,
        tx: TransactionView,
        outcome: MatchOutcome,
        intents: Mapping[UUID, IntentView],
        origin: SettlementOrigin,
        review_case_id: UUID | None = None,
        resolved_by: str | None = None,
    ) -> TransactionOutcomeView:
        """Apply ``outcome`` to the locked fact ``tx``.

        ``intents`` are the intents the caller already locked in this transaction (fact
        first, then intents, then the review case); a settle uses that view as the re-check
        and never locks again. ``review_case_id`` and ``resolved_by`` record operator
        provenance, which the database requires for ``operator_review`` settlements.
        """
        match outcome:
            case Settle():
                view = await self._settle(
                    uow, tx, outcome, intents, origin, review_case_id, resolved_by
                )
            case Review():
                view = await self._review(uow, tx, outcome)
            case NotApplicable():
                view = await self._not_applicable(uow, tx)
        try:
            await self._observer.on_outcome(uow, view)
        except Exception as exc:
            raise HandlerFailed("outcome observer failed") from exc
        return view

    def after_commit(self, view: TransactionOutcomeView, outcome: MatchOutcome) -> None:
        """Alerts and counters for an outcome whose transaction committed.

        Callers run this only after commit, so a rolled-back and retried attempt is never
        counted, and a failing metrics backend cannot touch the money transaction.
        """
        if isinstance(outcome, Review) and outcome.reason == ReviewReason.TENANT_MISMATCH:
            scope = outcome.details.get("scope", "")
            logger.error(
                "payment_tenant_mismatch",
                extra={
                    "transaction_id": str(view.transaction_id),
                    "review_case_id": str(view.review_case_id),
                    "scope": scope,
                },
            )
            increment_safely(
                self._metrics,
                "tenant_mismatch_total",
                {"tenant_id": view.tenant_id, "scope": scope},
            )
        elif isinstance(outcome, NotApplicable) and view.direction == Direction.UNKNOWN:
            increment_safely(
                self._metrics, "direction_unknown_total", {"tenant_id": view.tenant_id}
            )

    async def _settle(
        self,
        uow: UnitOfWork,
        tx: TransactionView,
        outcome: Settle,
        intents: Mapping[UUID, IntentView],
        origin: SettlementOrigin,
        review_case_id: UUID | None,
        resolved_by: str | None,
    ) -> TransactionOutcomeView:
        intent = intents.get(outcome.intent_id)
        if intent is None:
            raise PolicyViolation("a settle outcome must name an intent locked by the caller")
        ensure_settle_allowed(tx, intent, is_late=False)
        assert tx.receiving_account_id is not None and tx.merchant_id is not None
        now = self._clock.now()
        settlement_id = await uow.settlements.add(
            tenant_id=tx.tenant_id,
            environment=tx.environment,
            transaction_id=tx.id,
            intent_id=intent.id,
            receiving_account_id=tx.receiving_account_id,
            amount=tx.amount,
            intent_amount=intent.amount,
            origin=origin,
            settled_at=now,
            review_case_id=review_case_id,
            resolved_by=resolved_by,
        )
        transition_intent(intent.status, IntentStatus.PAID)
        if not await uow.intents.mark_paid(tx.tenant_id, intent.id, now):
            raise IllegalTransition("payment_intent", intent.status.value, IntentStatus.PAID.value)
        await self._move(uow, tx, MatchState.SETTLED)
        settled = SettlementView(
            settlement_id=settlement_id,
            tenant_id=tx.tenant_id,
            environment=tx.environment,
            merchant_id=tx.merchant_id,
            intent_id=intent.id,
            transaction_id=tx.id,
            receiving_account_id=tx.receiving_account_id,
            amount=tx.amount,
            origin=origin,
            settled_at=now,
            host_ref_type=intent.host_ref_type,
            host_ref_id=intent.host_ref_id,
        )
        try:
            await self._handler.on_settled(uow, settled)
        except Exception as exc:
            raise HandlerFailed("settlement handler failed") from exc
        event = PaymentSettled(
            event_id=uuid.uuid4(),
            tenant_id=tx.tenant_id,
            environment=tx.environment,
            merchant_id=tx.merchant_id,
            intent_id=intent.id,
            transaction_id=tx.id,
            settlement_id=settlement_id,
            receiving_account_id=tx.receiving_account_id,
            amount_vnd=tx.amount.value,
            origin=origin,
            settled_at=now,
            host_ref_type=intent.host_ref_type,
            host_ref_id=intent.host_ref_id,
        )
        await uow.outbox.add(outbox_view(event, now), INTENT_AGGREGATE, intent.id, now)
        logger.info(
            "payment_settled",
            extra={
                "transaction_id": str(tx.id),
                "intent_id": str(intent.id),
                "settlement_id": str(settlement_id),
                "origin": SettlementOrigin(origin).value,
            },
        )
        return self._view(
            tx, MatchState.SETTLED, intent_id=intent.id, review_case_id=review_case_id
        )

    async def _review(
        self, uow: UnitOfWork, tx: TransactionView, outcome: Review
    ) -> TransactionOutcomeView:
        now = self._clock.now()
        case_id = await uow.review_cases.find_open(tx.id)
        if case_id is None:
            case_id = await uow.review_cases.open(
                tenant_id=tx.tenant_id,
                environment=tx.environment,
                transaction_id=tx.id,
                reason=outcome.reason,
                details=outcome.details,
                opened_at=now,
                candidate_intent_id=outcome.candidate_intent_id,
            )
        else:
            await uow.review_cases.update_open(
                case_id, outcome.reason, outcome.details, outcome.candidate_intent_id
            )
        await self._move(uow, tx, MatchState.IN_REVIEW)
        event = PaymentNeedsReview(
            event_id=uuid.uuid4(),
            tenant_id=tx.tenant_id,
            environment=tx.environment,
            merchant_id=tx.merchant_id,
            transaction_id=tx.id,
            review_case_id=case_id,
            reason=outcome.reason,
            candidate_intent_id=outcome.candidate_intent_id,
            amount_vnd=tx.amount.value,
            direction=tx.direction,
        )
        await uow.outbox.add(outbox_view(event, now), TRANSACTION_AGGREGATE, tx.id, now)
        ids = {
            "transaction_id": str(tx.id),
            "review_case_id": str(case_id),
            "reason": outcome.reason.value,
        }
        logger.info("payment_needs_review", extra=ids)
        return self._view(
            tx,
            MatchState.IN_REVIEW,
            intent_id=outcome.candidate_intent_id,
            review_case_id=case_id,
            review_reason=outcome.reason,
        )

    async def _not_applicable(self, uow: UnitOfWork, tx: TransactionView) -> TransactionOutcomeView:
        await self._move(uow, tx, MatchState.NOT_APPLICABLE)
        return self._view(tx, MatchState.NOT_APPLICABLE)

    @staticmethod
    async def _move(uow: UnitOfWork, tx: TransactionView, target: MatchState) -> None:
        target = transition_match_state(tx.match_state, target)
        if target == tx.match_state:
            return
        if not await uow.transactions.set_match_state(tx.id, tx.match_state, target):
            raise IllegalTransition("provider_transaction", tx.match_state.value, target.value)

    @staticmethod
    def _view(
        tx: TransactionView,
        state: MatchState,
        *,
        intent_id: UUID | None = None,
        review_case_id: UUID | None = None,
        review_reason: ReviewReason | None = None,
    ) -> TransactionOutcomeView:
        return TransactionOutcomeView(
            transaction_id=tx.id,
            tenant_id=tx.tenant_id,
            environment=tx.environment,
            merchant_id=tx.merchant_id,
            match_state=state,
            direction=tx.direction,
            amount=tx.amount,
            intent_id=intent_id,
            review_case_id=review_case_id,
            review_reason=review_reason,
        )
