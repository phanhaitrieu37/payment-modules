"""Inbox processing: every matching outcome end to end, receiver resolution, quarantine,
retry/backoff, process_one, replay safety and requeue."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
import sqlalchemy as sa

from fakes.fake_provider import FakeProvider
from fakes.payment_app import App, Scope
from payment_module.application.config import PaymentModuleConfig
from payment_module.application.process_inbox import ProcessStatus
from payment_module.domain.enums import (
    InboxStatus,
    IntentStatus,
    LinkMethod,
    LinkStatus,
    MatchState,
    ObservationSource,
    ProcessingErrorCode,
    ReviewReason,
    SettlementOrigin,
)
from payment_module.domain.errors import IntentRejected
from payment_module.domain.matching.exact_amount_policy import ExactAmountPolicy
from payment_module.domain.review import MatchDecision

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


async def fact(app: App) -> sa.Row:
    [row] = await app.rows(app.tables.provider_transactions)
    return row


async def inbox(app: App) -> sa.Row:
    [row] = await app.rows(app.tables.webhook_inbox)
    return row


async def review(app: App) -> sa.Row:
    [row] = await app.rows(app.tables.review_cases)
    return row


async def outbox_types(app: App) -> list[str]:
    return [row.event_type for row in await app.rows(app.tables.outbox_events)]


async def intent_status(app: App, intent_id) -> str:
    t = app.tables.payment_intents
    [row] = await app.rows(t, t.c.id == intent_id)
    return row.status


async def test_exact_payment_settles(app: App) -> None:
    created = await app.intent()
    await app.pay(code=created.payment_reference, content=f"thanh toan {created.payment_reference}")

    row = await fact(app)
    assert row.match_state == MatchState.SETTLED.value
    assert row.receiving_account_id == app.m1.account_id
    assert row.merchant_id == app.m1.merchant_id
    assert row.dedup_key == f"fake|VCB|{app.m1.account_number}||webhook_id|{row.webhook_tx_id}"
    [settlement] = await app.rows(app.tables.settlements)
    assert settlement.intent_id == created.intent.id
    assert settlement.amount_vnd == settlement.intent_amount_vnd == 150_000
    assert settlement.origin == SettlementOrigin.AUTO.value
    assert await intent_status(app, created.intent.id) == IntentStatus.PAID.value
    assert (await inbox(app)).status == InboxStatus.PROCESSED.value
    assert await outbox_types(app) == ["PaymentSettled"]
    [call] = app.handler.calls
    assert call.intent_id == created.intent.id
    assert (call.host_ref_type, call.host_ref_id) == ("order", created.intent.host_ref_id)
    [outcome] = app.observer.outcomes
    assert outcome.match_state == MatchState.SETTLED

    [observation] = await app.rows(app.tables.provider_observations)
    assert observation.source == ObservationSource.WEBHOOK.value
    assert observation.transaction_id == row.id
    assert observation.link_method == LinkMethod.SAME_SOURCE_ID.value
    assert observation.link_status == LinkStatus.LINKED.value
    assert observation.memo == f"thanh toan {created.payment_reference}"
    assert observation.normalized is None


async def test_expired_intent_paid_on_time_still_settles(app: App) -> None:
    created = await app.intent(expires_at=app.clock.now() + timedelta(minutes=5))
    t = app.tables.payment_intents
    await app.execute(
        sa.update(t).where(t.c.id == created.intent.id).values(status=IntentStatus.EXPIRED.value)
    )
    await app.pay(code=created.payment_reference)
    assert (await fact(app)).match_state == MatchState.SETTLED.value


@pytest.mark.parametrize(("paid", "direction"), [(149_000, "under"), (151_000, "over")])
async def test_wrong_amount_opens_review(app: App, paid: int, direction: str) -> None:
    created = await app.intent()
    await app.pay(code=created.payment_reference, amount=paid)

    case = await review(app)
    assert case.reason == ReviewReason.AMOUNT_MISMATCH.value
    assert case.candidate_intent_id == created.intent.id
    assert case.details == {"direction": direction}
    assert (await fact(app)).match_state == MatchState.IN_REVIEW.value
    assert await app.count(app.tables.settlements) == 0
    assert await intent_status(app, created.intent.id) == IntentStatus.AWAITING_PAYMENT.value
    assert await outbox_types(app) == ["PaymentNeedsReview"]
    assert app.handler.calls == []


async def test_late_payment_opens_review(app: App) -> None:
    created = await app.intent(expires_at=app.clock.now() + timedelta(minutes=5))
    raw = app.payment(code=created.payment_reference)
    app.clock.advance(minutes=6)
    await app.webhook(raw)
    await app.module.process_inbox.run_batch()

    assert (await review(app)).reason == ReviewReason.LATE.value
    assert await app.count(app.tables.settlements) == 0


@pytest.mark.parametrize("direction", ["out", "unknown"])
async def test_outgoing_or_unknown_money_is_not_applicable(app: App, direction: str) -> None:
    created = await app.intent()
    await app.pay(code=created.payment_reference, direction=direction)

    assert (await fact(app)).match_state == MatchState.NOT_APPLICABLE.value
    assert await app.count(app.tables.review_cases) == 0
    assert await app.count(app.tables.settlements) == 0
    assert await outbox_types(app) == []
    assert (await inbox(app)).status == InboxStatus.PROCESSED.value
    assert app.metrics.counts.get("direction_unknown_total", 0) == (direction == "unknown")


async def test_unregistered_receiver_is_recorded_unbound(app: App) -> None:
    created = await app.intent()
    await app.pay(code=created.payment_reference, account="999999999")

    row = await fact(app)
    assert row.receiving_account_id is None and row.merchant_id is None
    case = await review(app)
    assert case.reason == ReviewReason.RECEIVER_UNBOUND.value
    assert case.candidate_intent_id is None


async def test_receiver_of_another_tenant_is_never_linked(app: App) -> None:
    created = await app.intent()
    await app.pay(code=created.payment_reference, account=app.mb.account_number)

    row = await fact(app)
    assert row.tenant_id == app.m1.tenant_id
    assert row.receiving_account_id is None and row.merchant_id is None
    assert (await review(app)).reason == ReviewReason.RECEIVER_UNBOUND.value


async def test_receiver_of_another_merchant_is_recorded_but_unbound(app: App) -> None:
    created = await app.intent()
    await app.pay(code=created.payment_reference, account=app.m2.account_number)

    row = await fact(app)
    assert row.receiving_account_id == app.m2.account_id
    assert row.merchant_id == app.m2.merchant_id
    assert (await review(app)).reason == ReviewReason.RECEIVER_UNBOUND.value
    assert await app.count(app.tables.settlements) == 0


async def test_account_not_bound_to_the_connection_is_unbound(app: App) -> None:
    created = await app.intent()
    await app.pay(code=created.payment_reference, account=app.m1_unbound_account)

    assert (await fact(app)).receiving_account_id is not None
    assert (await review(app)).reason == ReviewReason.RECEIVER_UNBOUND.value


async def test_no_reference_opens_review(app: App) -> None:
    await app.intent()
    await app.pay(code=None, content="chuyen tien")
    assert (await review(app)).reason == ReviewReason.NO_REFERENCE.value


async def test_two_references_are_ambiguous(app: App) -> None:
    first, second = await app.intent(), await app.intent()
    await app.pay(code=first.payment_reference, content=second.payment_reference)
    assert (await review(app)).reason == ReviewReason.AMBIGUOUS_REFERENCE.value


async def test_second_payment_for_a_paid_intent_is_already_paid(app: App) -> None:
    created = await app.intent()
    await app.pay(code=created.payment_reference)
    await app.pay(code=created.payment_reference)

    cases = await app.rows(app.tables.review_cases)
    assert [case.reason for case in cases] == [ReviewReason.ALREADY_PAID.value]
    assert cases[0].candidate_intent_id == created.intent.id
    assert await app.count(app.tables.settlements) == 1


async def test_cancelled_intent_opens_review(app: App) -> None:
    created = await app.intent()
    await app.module.cancel_intent.execute(app.m1.tenant_id, created.intent.id, "customer left")
    await app.pay(code=created.payment_reference)

    case = await review(app)
    assert case.reason == ReviewReason.INTENT_CANCELLED.value
    assert case.candidate_intent_id == created.intent.id


async def test_superseded_intent_points_review_at_the_successor(app: App) -> None:
    old, new = await app.intent(), await app.intent(amount_vnd=200_000)
    await app.module.cancel_intent.execute(
        app.m1.tenant_id, old.intent.id, "cart changed", superseded_by_intent_id=new.intent.id
    )
    await app.pay(code=old.payment_reference)

    case = await review(app)
    assert case.reason == ReviewReason.INTENT_SUPERSEDED.value
    assert case.candidate_intent_id == new.intent.id


async def test_reference_of_another_tenant_is_a_tenant_mismatch(app: App) -> None:
    foreign = await app.intent(app.mb)
    await app.pay(code=foreign.payment_reference)

    case = await review(app)
    assert case.reason == ReviewReason.TENANT_MISMATCH.value
    assert case.candidate_intent_id is None
    assert case.details == {"scope": "tenant"}
    assert case.tenant_id == app.m1.tenant_id
    assert await app.count(app.tables.settlements) == 0
    assert (await inbox(app)).status == InboxStatus.PROCESSED.value
    assert await intent_status(app, foreign.intent.id) == IntentStatus.AWAITING_PAYMENT.value
    assert app.metrics.counts["tenant_mismatch_total"] == 1
    assert await outbox_types(app) == ["PaymentNeedsReview"]


async def test_reference_of_another_environment_is_a_tenant_mismatch(app: App) -> None:
    live = await app.intent(app.m1_live)
    await app.pay(code=live.payment_reference)

    case = await review(app)
    assert case.reason == ReviewReason.TENANT_MISMATCH.value
    assert case.details == {"scope": "environment"}
    assert case.candidate_intent_id is None
    assert app.metrics.counts["tenant_mismatch_total"] == 1


async def test_reference_of_another_merchant_names_the_candidate(app: App) -> None:
    other = await app.intent(app.m2)
    await app.pay(code=other.payment_reference)

    case = await review(app)
    assert case.reason == ReviewReason.TENANT_MISMATCH.value
    assert case.details == {"scope": "receiving_account"}
    assert case.candidate_intent_id == other.intent.id


async def test_tenant_mismatch_is_logged_as_an_alert_with_ids_only(
    app: App, caplog: pytest.LogCaptureFixture
) -> None:
    foreign = await app.intent(app.mb)
    caplog.set_level("INFO", logger="payment_module")
    await app.pay(code=foreign.payment_reference, content="Nguyen Van A chuyen tien")

    [record] = [r for r in caplog.records if r.getMessage() == "payment_tenant_mismatch"]
    assert record.levelname == "ERROR"
    text = " ".join(f"{k}={v}" for k, v in vars(record).items())
    assert "Nguyen" not in text
    assert app.m1.account_number not in text
    assert foreign.payment_reference not in text


async def test_unparseable_verified_body_is_quarantined_not_retried(app: App) -> None:
    raw = b'{"id": 777, "amount": "not-a-number"}'
    await app.webhook(raw)
    [result] = await app.module.process_inbox.run_batch()

    assert (result.status, result.reason) == (ProcessStatus.PROCESSED, "quarantined")
    row = await inbox(app)
    assert row.status == InboxStatus.QUARANTINED.value
    assert row.last_error_code == "normalize_failed"
    assert await app.count(app.tables.provider_transactions) == 0
    assert app.metrics.counts["inbox_quarantined_total"] == 1


async def test_transient_failure_backs_off_then_fails(app: App) -> None:
    module = app.build(config=PaymentModuleConfig(worker_owner="w", inbox_max_attempts=3))
    created = await app.intent()
    app.handler.failures = 10
    await app.webhook(app.payment(code=created.payment_reference))

    [first] = await module.process_inbox.run_batch()
    assert first.reason == InboxStatus.RETRY_WAIT.value
    row = await inbox(app)
    assert (row.attempts, row.next_attempt_at) == (1, app.clock.now() + timedelta(seconds=2))
    assert row.last_error_code == ProcessingErrorCode.HANDLER_ERROR.value
    assert row.lease_owner is None

    assert await module.process_inbox.run_batch() == []  # not due yet
    app.clock.advance(seconds=2)
    [second] = await module.process_inbox.run_batch()
    assert second.reason == InboxStatus.RETRY_WAIT.value
    assert (await inbox(app)).next_attempt_at == app.clock.now() + timedelta(seconds=4)

    app.clock.advance(seconds=4)
    [third] = await module.process_inbox.run_batch()
    assert third.reason == InboxStatus.FAILED.value
    assert (await inbox(app)).status == InboxStatus.FAILED.value
    assert await app.count(app.tables.provider_transactions) == 0
    assert await app.count(app.tables.settlements) == 0


class _SettleEverything(ExactAmountPolicy):
    def decide(self, tx, intent, is_late):  # type: ignore[no-untyped-def]
        return MatchDecision.settle()


async def test_policy_violation_fails_immediately(app: App) -> None:
    module = app.build(policy=_SettleEverything())
    created = await app.intent()
    await app.webhook(app.payment(code=created.payment_reference, amount=1_000))
    [result] = await module.process_inbox.run_batch()

    assert result.reason == InboxStatus.FAILED.value
    row = await inbox(app)
    assert (row.status, row.last_error_code) == (InboxStatus.FAILED.value, "policy_violation")
    assert await app.count(app.tables.settlements) == 0


async def test_requeue_sends_failed_row_back(app: App) -> None:
    module = app.build(config=PaymentModuleConfig(worker_owner="w", inbox_max_attempts=1))
    created = await app.intent()
    app.handler.failures = 1
    ingested = await app.webhook(app.payment(code=created.payment_reference))
    await module.process_inbox.run_batch()
    assert (await inbox(app)).status == InboxStatus.FAILED.value

    assert await app.module.requeue_inbox.execute(ingested.inbox_id, "ops@host", "handler fixed")
    row = await inbox(app)
    assert (row.status, row.attempts, row.last_error_code) == (
        InboxStatus.RECEIVED.value,
        0,
        None,
    )
    await app.module.process_inbox.run_batch()
    assert (await fact(app)).match_state == MatchState.SETTLED.value
    assert not await app.module.requeue_inbox.execute(ingested.inbox_id, "ops@host", "again")


async def test_requeue_sends_quarantined_row_back(app: App) -> None:
    ingested = await app.webhook(b"garbage")
    assert await app.module.requeue_inbox.execute(ingested.inbox_id, "ops@host", "parser fixed")
    assert (await inbox(app)).status == InboxStatus.RECEIVED.value


async def test_process_one_settles_inline(app: App) -> None:
    created = await app.intent()
    ingested = await app.webhook(app.payment(code=created.payment_reference))
    result = await app.module.process_inbox.process_one(ingested.inbox_id)

    assert (result.status, result.reason) == (ProcessStatus.PROCESSED, "processed")
    assert (await fact(app)).match_state == MatchState.SETTLED.value
    again = await app.module.process_inbox.process_one(ingested.inbox_id)
    assert (again.status, again.reason) == (ProcessStatus.SKIPPED, "processed")


async def test_process_one_skips_a_row_the_worker_holds(app: App) -> None:
    ingested = await app.webhook(app.payment())
    async with app.uow_factory()() as uow:
        await uow.inbox.claim_batch(10, 60, "worker-a", app.clock.now())
        await uow.commit()
    result = await app.module.process_inbox.process_one(ingested.inbox_id)
    assert (result.status, result.reason) == (ProcessStatus.SKIPPED, "already_claimed")
    assert await app.count(app.tables.provider_transactions) == 0


async def test_process_one_does_not_wait_for_a_locked_row(app: App) -> None:
    ingested = await app.webhook(app.payment())
    t = app.tables.webhook_inbox
    async with app.engine.connect() as conn, conn.begin():
        await conn.execute(sa.select(t.c.id).where(t.c.id == ingested.inbox_id).with_for_update())
        result = await app.module.process_inbox.process_one(ingested.inbox_id)
    assert (result.status, result.reason) == (ProcessStatus.SKIPPED, "already_claimed")


async def test_process_one_of_unknown_row(app: App) -> None:
    result = await app.module.process_inbox.process_one(uuid.uuid4())
    assert (result.status, result.reason) == (ProcessStatus.SKIPPED, "not_found")


async def test_same_money_seen_again_is_not_applied_twice(app: App) -> None:
    created = await app.intent()
    raw = app.payment(tx_id=4242, code=created.payment_reference)
    await app.webhook(raw)
    await app.module.process_inbox.run_batch()

    # The same transaction stored again under another event key (for example requeued
    # from quarantine) must not produce a second outcome.
    t = app.tables.webhook_inbox
    [first] = await app.rows(t)
    await app.execute(
        t.insert().values(
            id=uuid.uuid4(),
            tenant_id=first.tenant_id,
            connection_id=first.connection_id,
            event_key="sha256:" + "a" * 64,
            event_key_kind="body_hash",
            body_sha256="a" * 64,
            raw_body=raw,
            received_at=app.clock.now(),
            status=InboxStatus.RECEIVED.value,
        )
    )
    [result] = await app.module.process_inbox.run_batch()

    assert result.reason == InboxStatus.PROCESSED.value
    assert await app.count(app.tables.provider_transactions) == 1
    assert await app.count(app.tables.provider_observations) == 1
    assert await app.count(app.tables.settlements) == 1
    assert len(app.handler.calls) == 1
    assert len(app.observer.outcomes) == 1


async def test_second_connection_sighting_links_to_the_same_fact(app: App) -> None:
    # A second active connection of m1 bound to the same account.
    second_id, locator = uuid.uuid4(), "b" * 32
    c = app.tables.provider_connections
    [conn_row] = await app.rows(c, c.c.id == app.m1.connection_id)
    await app.execute(
        c.insert().values({**conn_row._asdict(), "id": second_id, "locator": locator})
    )
    b = app.tables.connection_account_bindings
    await app.execute(
        b.insert().values(
            tenant_id=app.m1.tenant_id,
            merchant_id=app.m1.merchant_id,
            environment=app.m1.environment.value,
            connection_id=second_id,
            receiving_account_id=app.m1.account_id,
            created_by="test",
        )
    )
    other = Scope(
        app.m1.tenant_id,
        app.m1.merchant_id,
        app.m1.account_id,
        second_id,
        locator,
        app.m1.account_number,
        app.m1.environment,
    )
    created = await app.intent()
    raw = app.payment(tx_id=5151, code=created.payment_reference)
    await app.webhook(raw)
    await app.webhook(raw, other)
    await app.module.process_inbox.run_batch()

    assert await app.count(app.tables.provider_transactions) == 1
    observations = await app.rows(app.tables.provider_observations)
    assert len(observations) == 2
    assert len({o.transaction_id for o in observations}) == 1
    assert await app.count(app.tables.settlements) == 1
    assert len(app.handler.calls) == 1


class _BrokenAdapter(FakeProvider):
    def normalize(self, verified):  # type: ignore[no-untyped-def]
        raise RuntimeError("adapter bug")


async def test_unexpected_error_outside_host_code_is_a_transient_error(app: App) -> None:
    module = app.build(provider_registry={"fake": _BrokenAdapter()})
    await app.webhook(app.payment())
    [result] = await module.process_inbox.run_batch()

    assert result.reason == InboxStatus.RETRY_WAIT.value
    row = await inbox(app)
    assert row.last_error_code == ProcessingErrorCode.TRANSIENT_ERROR.value


async def test_observer_failure_is_a_handler_error(app: App) -> None:
    class Failing:
        async def on_outcome(self, uow, outcome) -> None:
            raise KeyError("projection bug")

    module = app.build(outcome_observer=Failing())
    await app.webhook(app.payment(direction="out"))
    await module.process_inbox.run_batch()
    assert (await inbox(app)).last_error_code == ProcessingErrorCode.HANDLER_ERROR.value


async def test_persisted_error_codes_come_from_the_fixed_vocabulary(app: App) -> None:
    allowed = {code.value for code in ProcessingErrorCode}
    await app.webhook(b"garbage")
    await app.webhook(b'{"id": 1, "amount": "x"}')
    app.handler.failures = 1
    created = await app.intent()
    await app.webhook(app.payment(code=created.payment_reference))
    await app.module.process_inbox.run_batch()
    codes = {row.last_error_code for row in await app.rows(app.tables.webhook_inbox)}
    assert codes - {None} <= allowed
    assert codes - {None} == {"no_event_key", "normalize_failed", "handler_error"}


async def test_disabled_but_bound_account_still_settles_an_issued_intent(app: App) -> None:
    """The payer paid the beneficiary the module showed; disabling the account only stops
    new intents."""
    created = await app.intent()
    t = app.tables.receiving_accounts
    await app.execute(sa.update(t).where(t.c.id == app.m1.account_id).values(status="disabled"))
    await app.pay(code=created.payment_reference)

    assert (await fact(app)).match_state == MatchState.SETTLED.value
    assert await intent_status(app, created.intent.id) == IntentStatus.PAID.value
    with pytest.raises(IntentRejected) as caught:
        await app.intent()
    assert caught.value.code == "RECEIVING_ACCOUNT_NOT_READY"


async def test_delayed_worker_keeps_the_receipt_time_for_a_later_rematch(app: App) -> None:
    """Received before expiry, processed after it while the receiver was unbound: once the
    account is bound again, the rematch uses the inbox receipt time and settles, not LATE."""
    created = await app.intent(expires_at=app.clock.now() + timedelta(minutes=1))
    bindings = app.tables.connection_account_bindings
    await app.execute(
        sa.delete(bindings).where(bindings.c.receiving_account_id == app.m1.account_id)
    )
    app.clock.advance(seconds=30)
    received_at = app.clock.now()
    await app.webhook(app.payment(code=created.payment_reference))
    app.clock.advance(minutes=6)
    await app.module.process_inbox.run_batch()

    assert (await review(app)).reason == ReviewReason.RECEIVER_UNBOUND.value
    [observation] = await app.rows(app.tables.provider_observations)
    assert observation.observed_at == received_at

    await app.execute(
        bindings.insert().values(
            tenant_id=app.m1.tenant_id,
            merchant_id=app.m1.merchant_id,
            environment=app.m1.environment.value,
            connection_id=app.m1.connection_id,
            receiving_account_id=app.m1.account_id,
            created_by="test",
        )
    )
    [result] = await app.module.rematch_unbound.execute(
        app.m1.tenant_id, app.m1.connection_id, "ops@test"
    )

    assert result.match_state == MatchState.SETTLED
    assert (await fact(app)).match_state == MatchState.SETTLED.value
    assert await intent_status(app, created.intent.id) == IntentStatus.PAID.value
