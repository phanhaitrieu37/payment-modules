"""ApplyMatchOutcome on PostgreSQL: atomic writes, review upsert, operator provenance,
observer/handler rollback, and the database as the last line on amount and scope."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from fakes.payment_app import App
from payment_module.domain.enums import (
    Direction,
    FirstSource,
    IdentityKind,
    IntentStatus,
    MatchState,
    ReviewReason,
    SettlementOrigin,
)
from payment_module.domain.errors import PolicyViolation
from payment_module.domain.intent import IntentView
from payment_module.domain.money import AmountVnd
from payment_module.domain.review import NotApplicable, Review, Settle
from payment_module.ports.unit_of_work import NewProviderTransaction

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


async def new_fact(app: App, uow, *, amount: int = 150_000, direction=Direction.IN):
    tx, _ = await uow.transactions.insert_or_get_by_dedup_key(
        NewProviderTransaction(
            tenant_id=app.m1.tenant_id,
            environment=app.m1.environment,
            provider="fake",
            provider_account_key=f"VCB|{app.m1.account_number}|",
            dedup_key=f"fake|{uuid.uuid4().hex}",
            identity_kind=IdentityKind.WEBHOOK_ID,
            identity_value=uuid.uuid4().hex,
            receiving_account_id=app.m1.account_id,
            merchant_id=app.m1.merchant_id,
            amount=AmountVnd(amount),
            direction=direction,
            bank_reference=None,
            first_source=FirstSource.WEBHOOK,
        )
    )
    return tx


async def locked(uow, intent_id) -> dict[uuid.UUID, IntentView]:
    intent = await uow.intents.get_for_update("tenant-a", intent_id)
    return {intent.id: intent}


async def test_review_updates_the_open_case_instead_of_opening_another(app: App) -> None:
    created = await app.intent()
    apply = app.module.apply_outcome
    async with app.uow_factory()() as uow:
        tx = await new_fact(app, uow)
        await apply.apply(
            uow,
            tx=tx,
            outcome=Review(ReviewReason.AMOUNT_MISMATCH, created.intent.id, {"direction": "under"}),
            intents={},
            origin=SettlementOrigin.AUTO,
        )
        tx = await uow.transactions.get_for_update(tx.tenant_id, tx.environment, tx.id)
        await apply.apply(
            uow, tx=tx, outcome=Review(ReviewReason.LATE), intents={}, origin=SettlementOrigin.AUTO
        )
        await uow.commit()

    [case] = await app.rows(app.tables.review_cases)
    assert (case.reason, case.details, case.candidate_intent_id) == ("LATE", {}, None)
    assert await app.count(app.tables.outbox_events) == 2


async def test_operator_settlement_carries_review_provenance(app: App) -> None:
    created = await app.intent()
    apply = app.module.apply_outcome
    async with app.uow_factory()() as uow:
        tx = await new_fact(app, uow)
        view = await apply.apply(
            uow,
            tx=tx,
            outcome=Review(ReviewReason.LATE, created.intent.id),
            intents={},
            origin=SettlementOrigin.AUTO,
        )
        tx = await uow.transactions.get_for_update(tx.tenant_id, tx.environment, tx.id)
        assert tx.match_state == MatchState.IN_REVIEW
        await apply.apply(
            uow,
            tx=tx,
            outcome=Settle(created.intent.id),
            intents=await locked(uow, created.intent.id),
            origin=SettlementOrigin.OPERATOR_REVIEW,
            review_case_id=view.review_case_id,
            resolved_by="ops@host",
        )
        await uow.commit()

    [settlement] = await app.rows(app.tables.settlements)
    assert settlement.origin == SettlementOrigin.OPERATOR_REVIEW.value
    assert (settlement.review_case_id, settlement.resolved_by) == (view.review_case_id, "ops@host")
    [row] = await app.rows(app.tables.provider_transactions)
    assert row.match_state == MatchState.SETTLED.value


async def test_operator_settlement_without_provenance_is_refused_by_the_database(app: App) -> None:
    created = await app.intent()
    with pytest.raises(IntegrityError):
        async with app.uow_factory()() as uow:
            tx = await new_fact(app, uow)
            await app.module.apply_outcome.apply(
                uow,
                tx=tx,
                outcome=Settle(created.intent.id),
                intents=await locked(uow, created.intent.id),
                origin=SettlementOrigin.OPERATOR_REVIEW,
            )
    assert await app.count(app.tables.settlements) == 0


async def test_settle_needs_an_intent_locked_by_the_caller(app: App) -> None:
    created = await app.intent()
    async with app.uow_factory()() as uow:
        tx = await new_fact(app, uow)
        with pytest.raises(PolicyViolation):
            await app.module.apply_outcome.apply(
                uow,
                tx=tx,
                outcome=Settle(created.intent.id),
                intents={},
                origin=SettlementOrigin.AUTO,
            )


async def test_settle_rechecks_amount_on_the_locked_intent(app: App) -> None:
    created = await app.intent()
    async with app.uow_factory()() as uow:
        tx = await new_fact(app, uow, amount=100_000)
        with pytest.raises(PolicyViolation):
            await app.module.apply_outcome.apply(
                uow,
                tx=tx,
                outcome=Settle(created.intent.id),
                intents=await locked(uow, created.intent.id),
                origin=SettlementOrigin.AUTO,
            )


async def test_observer_failure_rolls_back_the_settlement(app: App) -> None:
    class Failing:
        async def on_outcome(self, uow, outcome) -> None:
            raise RuntimeError("projection failed")

    module = app.build(outcome_observer=Failing())
    created = await app.intent()
    with pytest.raises(RuntimeError):
        async with app.uow_factory()() as uow:
            tx = await new_fact(app, uow)
            await module.apply_outcome.apply(
                uow,
                tx=tx,
                outcome=Settle(created.intent.id),
                intents=await locked(uow, created.intent.id),
                origin=SettlementOrigin.AUTO,
            )
            await uow.commit()
    assert await app.count(app.tables.settlements) == 0
    assert await app.count(app.tables.outbox_events) == 0
    t = app.tables.payment_intents
    [row] = await app.rows(t, t.c.id == created.intent.id)
    assert row.status == IntentStatus.AWAITING_PAYMENT.value


async def test_not_applicable_writes_no_event(app: App) -> None:
    async with app.uow_factory()() as uow:
        tx = await new_fact(app, uow, direction=Direction.OUT)
        view = await app.module.apply_outcome.apply(
            uow, tx=tx, outcome=NotApplicable(), intents={}, origin=SettlementOrigin.AUTO
        )
        await uow.commit()
    assert view.match_state == MatchState.NOT_APPLICABLE
    assert await app.count(app.tables.outbox_events) == 0
    assert await app.count(app.tables.review_cases) == 0


async def test_settled_event_payload_has_no_personal_data(app: App) -> None:
    created = await app.intent()
    await app.pay(code=created.payment_reference, content="Nguyen Van A")
    [event] = await app.rows(app.tables.outbox_events)
    text = str(event.payload) + str(event.trusted_scope)
    assert "Nguyen" not in text and app.m1.account_number not in text
    assert event.trusted_scope == {
        "tenant_id": app.m1.tenant_id,
        "merchant_id": str(app.m1.merchant_id),
        "environment": app.m1.environment.value,
    }
    assert event.aggregate_type == "payment_intent"
    assert event.aggregate_id == created.intent.id
