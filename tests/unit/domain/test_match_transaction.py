from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from payment_module.domain.enums import (
    Direction,
    Environment,
    IntentStatus,
    ReceiptTimeSource,
    ReviewReason,
)
from payment_module.domain.errors import PolicyViolation
from payment_module.domain.intent import IntentView
from payment_module.domain.matching.invariant_guard import ConnectionView
from payment_module.domain.matching.match_transaction import (
    MatchContext,
    MatchTransaction,
    ensure_settle_allowed,
)
from payment_module.domain.money import AmountVnd
from payment_module.domain.reference import tokens_from
from payment_module.domain.review import MatchDecision, NotApplicable, Review, Settle
from payment_module.domain.transaction import TransactionView


@dataclass
class SpyPolicy:
    """Records every call and returns a fixed decision."""

    decision: object
    calls: list[tuple[UUID, bool]] = field(default_factory=list)

    def decide(self, tx: TransactionView, intent: IntentView, is_late: bool) -> MatchDecision:
        self.calls.append((intent.id, is_late))
        return self.decision  # type: ignore[return-value]


@pytest.fixture
def context(make_tx, make_intent, connection, account_id, now):
    def build(
        *,
        tx: TransactionView | None = None,
        intents: tuple[IntentView, ...] | None = None,
        memo: str | None = None,
        received_at=None,
        time_source: ReceiptTimeSource = ReceiptTimeSource.WEBHOOK_RECEIVED,
        bound: frozenset[UUID] | None = None,
    ) -> MatchContext:
        intents = (make_intent(),) if intents is None else intents
        memo = f"thanh toan {intents[0].payment_reference}" if memo is None and intents else memo
        return MatchContext(
            tx=tx or make_tx(),
            connection=connection,
            bound_account_ids=frozenset({account_id}) if bound is None else bound,
            tokens=tuple(tokens_from(None, memo)),
            candidates={intent.payment_reference: intent for intent in intents},
            effective_received_at=received_at or now,
            time_source=time_source,
        )

    return build


def test_review_reason_has_exactly_ten_values() -> None:
    assert [reason.value for reason in ReviewReason] == [
        "RECEIVER_UNBOUND",
        "NO_REFERENCE",
        "AMBIGUOUS_REFERENCE",
        "TENANT_MISMATCH",
        "ALREADY_PAID",
        "INTENT_CANCELLED",
        "INTENT_SUPERSEDED",
        "AMOUNT_MISMATCH",
        "LATE",
        "UNVERIFIED_IDENTITY",
    ]


def test_exact_payment_settles(context) -> None:
    ctx = context()
    intent = next(iter(ctx.candidates.values()))
    assert MatchTransaction().decide(ctx) == Settle(intent.id)


@pytest.mark.parametrize("direction", [Direction.OUT, Direction.UNKNOWN])
@pytest.mark.parametrize(
    "time_source", [ReceiptTimeSource.WEBHOOK_RECEIVED, ReceiptTimeSource.OBSERVED]
)
@pytest.mark.parametrize("receiver_bound", [True, False])
def test_outgoing_or_unknown_is_not_applicable_from_any_source(
    context, make_tx, account_id, direction, time_source, receiver_bound
) -> None:
    tx = make_tx(
        direction=direction,
        receiving_account_id=account_id if receiver_bound else None,
        merchant_id=make_tx().merchant_id if receiver_bound else None,
    )
    policy = SpyPolicy(MatchDecision.settle())
    outcome = MatchTransaction(policy).decide(context(tx=tx, time_source=time_source))
    assert outcome == NotApplicable()
    assert policy.calls == []


def test_unbound_receiver_goes_to_review(context, make_tx) -> None:
    ctx = context(tx=make_tx(receiving_account_id=None, merchant_id=None))
    assert MatchTransaction().decide(ctx) == Review(ReviewReason.RECEIVER_UNBOUND)


def test_account_not_bound_to_connection_goes_to_review(context) -> None:
    ctx = context(bound=frozenset({uuid4()}))
    assert MatchTransaction().decide(ctx) == Review(ReviewReason.RECEIVER_UNBOUND)


def test_no_reference_goes_to_review(context) -> None:
    ctx = context(memo="chuyen tien")
    assert MatchTransaction().decide(ctx) == Review(ReviewReason.NO_REFERENCE)


def test_same_prefix_other_code_is_not_a_match(context, make_intent) -> None:
    ctx = context(intents=(make_intent(),), memo="SUBK7Q2M8")
    assert MatchTransaction().decide(ctx) == Review(ReviewReason.NO_REFERENCE)


def test_two_intents_in_memo_are_ambiguous(context, make_intent) -> None:
    first = make_intent()
    second = make_intent(id=uuid4(), payment_reference="TOPA1B2C3")
    ctx = context(intents=(first, second), memo=f"{first.payment_reference} TOPA1B2C3")
    assert MatchTransaction().decide(ctx) == Review(ReviewReason.AMBIGUOUS_REFERENCE)


def test_other_tenant_intent_review_without_candidate(context, make_intent) -> None:
    policy = SpyPolicy(MatchDecision.settle())
    ctx = context(intents=(make_intent(tenant_id="tenant-b"),))
    outcome = MatchTransaction(policy).decide(ctx)
    assert outcome == Review(ReviewReason.TENANT_MISMATCH, None, {"scope": "tenant"})
    assert policy.calls == []


def test_test_intent_live_fact_not_settled(context, make_intent) -> None:
    policy = SpyPolicy(MatchDecision.settle())
    ctx = context(intents=(make_intent(environment=Environment.TEST),))
    outcome = MatchTransaction(policy).decide(ctx)
    assert outcome == Review(ReviewReason.TENANT_MISMATCH, None, {"scope": "environment"})
    assert policy.calls == []


def test_same_tenant_other_account_not_settled(context, make_intent) -> None:
    # Intent of another merchant of the same tenant, paid into this merchant's account.
    policy = SpyPolicy(MatchDecision.settle())
    other = make_intent(merchant_id=uuid4(), receiving_account_id=uuid4())
    outcome = MatchTransaction(policy).decide(context(intents=(other,)))
    assert outcome == Review(ReviewReason.TENANT_MISMATCH, other.id, {"scope": "receiving_account"})
    assert policy.calls == []


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (IntentStatus.PAID, ReviewReason.ALREADY_PAID),
        (IntentStatus.CANCELLED, ReviewReason.INTENT_CANCELLED),
    ],
)
def test_closed_intent_goes_to_review(context, make_intent, status, reason) -> None:
    policy = SpyPolicy(MatchDecision.settle())
    intent = make_intent(status=status)
    outcome = MatchTransaction(policy).decide(context(intents=(intent,)))
    assert outcome == Review(reason, intent.id)
    assert policy.calls == []


def test_superseded_intent_points_at_replacement(context, make_intent) -> None:
    replacement = uuid4()
    intent = make_intent(status=IntentStatus.SUPERSEDED, superseded_by_intent_id=replacement)
    outcome = MatchTransaction().decide(context(intents=(intent,)))
    assert outcome == Review(ReviewReason.INTENT_SUPERSEDED, replacement)


@pytest.mark.parametrize(("amount", "direction"), [(100_000, "under"), (200_000, "over")])
def test_amount_mismatch_goes_to_review(context, make_tx, make_intent, amount, direction) -> None:
    intent = make_intent()
    ctx = context(tx=make_tx(amount=AmountVnd(amount)), intents=(intent,))
    assert MatchTransaction().decide(ctx) == Review(
        ReviewReason.AMOUNT_MISMATCH, intent.id, {"direction": direction}
    )


def test_late_webhook_goes_to_review(context, make_intent) -> None:
    intent = make_intent()
    ctx = context(intents=(intent,), received_at=intent.expires_at + timedelta(seconds=1))
    assert MatchTransaction().decide(ctx) == Review(ReviewReason.LATE, intent.id)


def test_expired_intent_paid_after_expiry_goes_to_late_review(context, make_intent) -> None:
    intent = make_intent(status=IntentStatus.EXPIRED)
    ctx = context(intents=(intent,), received_at=intent.expires_at + timedelta(seconds=1))
    assert MatchTransaction().decide(ctx) == Review(ReviewReason.LATE, intent.id)


def test_expired_intent_paid_before_expiry_settles(context, make_intent) -> None:
    # A payment received before expires_at settles even if the expiry job already ran.
    intent = make_intent(status=IntentStatus.EXPIRED)
    ctx = context(intents=(intent,), received_at=intent.expires_at - timedelta(seconds=1))
    assert MatchTransaction().decide(ctx) == Settle(intent.id)


def test_expired_intent_paid_before_expiry_with_wrong_amount_is_reviewed(
    context, make_tx, make_intent
) -> None:
    intent = make_intent(status=IntentStatus.EXPIRED)
    ctx = context(
        tx=make_tx(amount=AmountVnd(149_000)),
        intents=(intent,),
        received_at=intent.expires_at - timedelta(seconds=1),
    )
    assert MatchTransaction().decide(ctx) == Review(
        ReviewReason.AMOUNT_MISMATCH, intent.id, {"direction": "under"}
    )


def test_api_only_fact_after_expiry_without_verified_time_is_not_settled(
    context, make_intent
) -> None:
    # Without a verified provider time the application uses the observation time, which is
    # after expiry, so the intent can only be reviewed.
    intent = make_intent()
    ctx = context(
        intents=(intent,),
        received_at=intent.expires_at + timedelta(minutes=30),
        time_source=ReceiptTimeSource.OBSERVED,
    )
    assert MatchTransaction().decide(ctx) == Review(ReviewReason.LATE, intent.id)


def test_policy_sees_late_flag(context, make_intent) -> None:
    intent = make_intent(status=IntentStatus.EXPIRED)
    policy = SpyPolicy(MatchDecision.review(ReviewReason.LATE))
    late = intent.expires_at + timedelta(seconds=1)
    MatchTransaction(policy).decide(context(intents=(intent,), received_at=late))
    assert policy.calls == [(intent.id, True)]


def test_policy_may_tighten(context) -> None:
    ctx = context()
    intent = next(iter(ctx.candidates.values()))
    policy = SpyPolicy(MatchDecision.review(ReviewReason.LATE, {"rule": "manual"}))
    assert MatchTransaction(policy).decide(ctx) == Review(
        ReviewReason.LATE, intent.id, {"rule": "manual"}
    )


def test_policy_cannot_widen_amount(context, make_tx) -> None:
    ctx = context(tx=make_tx(amount=AmountVnd(149_000)))
    with pytest.raises(PolicyViolation):
        MatchTransaction(SpyPolicy(MatchDecision.settle())).decide(ctx)


def test_policy_cannot_widen_lateness(context, make_intent) -> None:
    intent = make_intent()
    ctx = context(intents=(intent,), received_at=intent.expires_at + timedelta(hours=1))
    with pytest.raises(PolicyViolation):
        MatchTransaction(SpyPolicy(MatchDecision.settle())).decide(ctx)


@pytest.mark.parametrize(
    "reason",
    [
        reason
        for reason in ReviewReason
        if reason not in {ReviewReason.AMOUNT_MISMATCH, ReviewReason.LATE}
    ],
)
def test_policy_cannot_widen_with_core_reason(context, reason) -> None:
    with pytest.raises(PolicyViolation):
        MatchTransaction(SpyPolicy(MatchDecision.review(reason))).decide(context())


def test_policy_cannot_widen_with_foreign_result(context) -> None:
    with pytest.raises(PolicyViolation):
        MatchTransaction(SpyPolicy("SETTLE")).decide(context())


def test_operator_accept_late_still_needs_exact_amount(make_tx, make_intent) -> None:
    intent = make_intent(status=IntentStatus.EXPIRED)
    ensure_settle_allowed(make_tx(), intent, is_late=True, allow_late=True)
    with pytest.raises(PolicyViolation):
        ensure_settle_allowed(
            make_tx(amount=AmountVnd(150_001)), intent, is_late=True, allow_late=True
        )


@pytest.mark.parametrize(
    "tx_changes",
    [
        {"direction": Direction.OUT},
        {"receiving_account_id": uuid4()},
        {"environment": Environment.TEST},
        {"tenant_id": "tenant-b"},
    ],
)
def test_post_check_rejects_wrong_direction_or_scope(make_tx, make_intent, tx_changes) -> None:
    with pytest.raises(PolicyViolation):
        ensure_settle_allowed(make_tx(**tx_changes), make_intent(), is_late=False)


@pytest.mark.parametrize(
    "status", [IntentStatus.PAID, IntentStatus.CANCELLED, IntentStatus.SUPERSEDED]
)
def test_post_check_rejects_closed_intent(make_tx, make_intent, status) -> None:
    with pytest.raises(PolicyViolation):
        ensure_settle_allowed(make_tx(), make_intent(status=status), is_late=False, allow_late=True)


def test_context_requires_aware_receipt_time(context, now) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        context(received_at=now.replace(tzinfo=None))


def test_plain_string_enum_fields_still_settle(connection, account_id, now) -> None:
    # Storage rows carry enum columns as plain strings.
    tx = TransactionView(
        id=uuid4(),
        tenant_id=connection.tenant_id,
        environment="live",  # type: ignore[arg-type]
        provider_account_key="VCB|0123456789|",
        receiving_account_id=account_id,
        merchant_id=connection.merchant_id,
        amount=AmountVnd(150_000),
        direction="in",  # type: ignore[arg-type]
        match_state="recorded",  # type: ignore[arg-type]
    )
    intent = IntentView(
        id=uuid4(),
        tenant_id=connection.tenant_id,
        merchant_id=connection.merchant_id,
        environment="live",  # type: ignore[arg-type]
        receiving_account_id=account_id,
        amount=AmountVnd(150_000),
        status="awaiting_payment",  # type: ignore[arg-type]
        payment_reference="SUBPLAIN1",
        expires_at=now + timedelta(minutes=5),
        host_ref_type="order",
        host_ref_id="order-1",
    )
    plain_connection = ConnectionView(
        connection.id,
        connection.tenant_id,
        connection.merchant_id,
        "live",  # type: ignore[arg-type]
    )
    ctx = MatchContext(
        tx=tx,
        connection=plain_connection,
        bound_account_ids=frozenset({account_id}),
        tokens=("SUBPLAIN1",),
        candidates={"SUBPLAIN1": intent},
        effective_received_at=now,
        time_source="webhook_received",  # type: ignore[arg-type]
    )
    assert tx.direction is Direction.IN
    assert intent.status is IntentStatus.AWAITING_PAYMENT
    assert MatchTransaction().decide(ctx) == Settle(intent.id)


def test_plain_string_outgoing_is_not_applicable(context, make_tx) -> None:
    tx = make_tx(direction="out")
    assert MatchTransaction().decide(context(tx=tx)) == NotApplicable()


@pytest.mark.parametrize(
    ("build", "field", "value"),
    [
        ("make_tx", "direction", "incoming"),
        ("make_tx", "environment", "prod"),
        ("make_intent", "status", "open"),
        ("make_intent", "environment", "LIVE"),
    ],
)
def test_unknown_enum_string_raises(request, build, field, value) -> None:
    with pytest.raises(ValueError):
        request.getfixturevalue(build)(**{field: value})


def test_unknown_connection_environment_raises(connection) -> None:
    with pytest.raises(ValueError):
        ConnectionView(connection.id, connection.tenant_id, connection.merchant_id, "prod")  # type: ignore[arg-type]
