"""Domain service shared by inbox processing, reconciliation and review rematch.

The chain is fixed: guard -> reference resolver (with scope check) -> intent eligibility ->
matching policy -> core post-check. Only the policy is injected. The service works on data
the application has already loaded and locked; it never queries storage itself.

An operator review may pass an :class:`OperatorOverride`: it can name the intent instead of
the memo reference, or accept late money. It never skips the guard, the scope check, the
eligibility check or the exact-amount post-check.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from payment_module.domain.enums import (
    DecisionOutcome,
    Direction,
    IntentStatus,
    ReceiptTimeSource,
    ReviewReason,
    coerce_enum_fields,
)
from payment_module.domain.errors import PolicyViolation
from payment_module.domain.intent import IntentView, ensure_aware
from payment_module.domain.matching.exact_amount_policy import ExactAmountPolicy, MatchingPolicy
from payment_module.domain.matching.intent_eligibility import Ineligible, IntentEligibility
from payment_module.domain.matching.invariant_guard import (
    ConnectionView,
    GuardResult,
    InvariantGuard,
)
from payment_module.domain.matching.reference_resolver import (
    ReferenceResolver,
    ResolutionKind,
    ScopeMismatch,
)
from payment_module.domain.review import (
    MatchDecision,
    MatchOutcome,
    NotApplicable,
    Review,
    Settle,
)
from payment_module.domain.transaction import TransactionView

_POLICY_REVIEW_REASONS = frozenset({ReviewReason.AMOUNT_MISMATCH, ReviewReason.LATE})
_SETTLEABLE_STATUSES = frozenset({IntentStatus.AWAITING_PAYMENT, IntentStatus.EXPIRED})


@dataclass(frozen=True, slots=True)
class MatchContext:
    """Everything one matching decision needs, loaded by the application.

    ``tokens`` come from ``tokens_from(code, content)``. ``candidates`` maps a normalized
    reference to its intent. The application picks ``effective_received_at``: the inbox
    receipt time for webhooks, a provider time only once its format is verified, otherwise
    the observation time. ``time_source`` records which one was used.
    """

    tx: TransactionView
    connection: ConnectionView
    bound_account_ids: frozenset[UUID]
    tokens: tuple[str, ...]
    candidates: Mapping[str, IntentView]
    effective_received_at: datetime
    time_source: ReceiptTimeSource

    def __post_init__(self) -> None:
        coerce_enum_fields(self, time_source=ReceiptTimeSource)
        ensure_aware(self.effective_received_at, "effective_received_at")


@dataclass(frozen=True, slots=True)
class OperatorOverride:
    """An audited operator decision fed into matching.

    ``forced_intent_id`` replaces reference resolution with this intent, which the caller
    must have locked and put into ``MatchContext.candidates``. ``accept_late`` lets money
    received after the intent expired settle; the amount must still be exact.
    """

    forced_intent_id: UUID | None = None
    accept_late: bool = False


def ensure_settle_allowed(
    tx: TransactionView, intent: IntentView, is_late: bool, *, allow_late: bool = False
) -> None:
    """Core post-check: raise :class:`PolicyViolation` unless settling is safe.

    Settling needs incoming money of exactly the intent amount, into the intent's own
    receiving account in the same tenant and environment, for an intent that is still open.
    Late money needs ``allow_late``, which only the audited operator path may pass.
    """
    if tx.direction != Direction.IN:
        raise PolicyViolation("only incoming money can settle an intent")
    same_scope = (
        tx.tenant_id == intent.tenant_id
        and tx.environment == intent.environment
        and tx.merchant_id == intent.merchant_id
        and tx.receiving_account_id == intent.receiving_account_id
    )
    if not same_scope:
        raise PolicyViolation("transaction and intent are not in the same receiving scope")
    if intent.status not in _SETTLEABLE_STATUSES:
        raise PolicyViolation(f"intent in status {intent.status.value} cannot be settled")
    if tx.amount != intent.amount:
        raise PolicyViolation("settlement amount must equal the intent amount")
    if is_late and not allow_late:
        raise PolicyViolation("late money needs an operator decision")


class MatchTransaction:
    def __init__(self, policy: MatchingPolicy | None = None) -> None:
        self._guard = InvariantGuard()
        self._resolver = ReferenceResolver()
        self._eligibility = IntentEligibility()
        self._policy: MatchingPolicy = policy if policy is not None else ExactAmountPolicy()

    def decide(
        self, ctx: MatchContext, operator_override: OperatorOverride | None = None
    ) -> MatchOutcome:
        tx = ctx.tx
        guard = self._guard.check(tx, ctx.bound_account_ids, ctx.connection)
        if guard == GuardResult.OUTGOING:
            return NotApplicable()
        if guard == GuardResult.RECEIVER_UNBOUND:
            return Review(ReviewReason.RECEIVER_UNBOUND)

        override = operator_override or OperatorOverride()
        if override.forced_intent_id is not None:
            intent = _forced_intent(ctx, override.forced_intent_id)
        else:
            resolution = self._resolver.resolve(ctx.tokens, ctx.candidates)
            if resolution.kind == ResolutionKind.NONE:
                return Review(ReviewReason.NO_REFERENCE)
            if resolution.kind == ResolutionKind.AMBIGUOUS or resolution.intent is None:
                return Review(ReviewReason.AMBIGUOUS_REFERENCE)
            intent = resolution.intent

        scope = self._resolver.scope_mismatch(tx, intent)
        if scope is not None:
            # Test and Live are isolated like tenants: nothing about an intent of another
            # tenant or environment may leak into this review. Only a same-scope intent on
            # another receiving account is offered as a candidate.
            candidate = intent.id if scope == ScopeMismatch.RECEIVING_ACCOUNT else None
            return Review(ReviewReason.TENANT_MISMATCH, candidate, {"scope": scope.value})

        eligibility = self._eligibility.check(intent, ctx.effective_received_at)
        if isinstance(eligibility, Ineligible):
            return Review(eligibility.reason, eligibility.candidate_intent_id)

        decision = self._policy.decide(tx, intent, eligibility.is_late)
        outcome = self._post_check(tx, intent, eligibility.is_late, decision)
        if (
            override.accept_late
            and isinstance(outcome, Review)
            and outcome.reason == ReviewReason.LATE
        ):
            # Only lateness is waived. The post-check still requires incoming money of the
            # exact amount into the intent's own scope, so an amount mismatch never passes,
            # even from a policy that reported it as late.
            if tx.amount != intent.amount:
                return Review(ReviewReason.AMOUNT_MISMATCH, intent.id)
            ensure_settle_allowed(tx, intent, is_late=True, allow_late=True)
            return Settle(intent.id)
        return outcome

    @staticmethod
    def _post_check(
        tx: TransactionView, intent: IntentView, is_late: bool, decision: object
    ) -> MatchOutcome:
        if not isinstance(decision, MatchDecision):
            raise PolicyViolation("matching policy must return a MatchDecision")
        if decision.outcome == DecisionOutcome.SETTLE:
            ensure_settle_allowed(tx, intent, is_late)
            return Settle(intent.id)
        reason = decision.reason
        if reason is None or reason not in _POLICY_REVIEW_REASONS:
            raise PolicyViolation(f"matching policy cannot use review reason {reason}")
        return Review(reason, intent.id, decision.details)


def _forced_intent(ctx: MatchContext, intent_id: UUID) -> IntentView:
    for intent in ctx.candidates.values():
        if intent.id == intent_id:
            return intent
    raise PolicyViolation("an operator-chosen intent must be loaded as a candidate")
