"""An intent's merchant, receiving account and amount never change after creation: every
lifecycle write and a refused idempotent replay leave them as they were stored."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import timedelta

import pytest

from fakes.payment_app import App
from payment_module.domain.enums import IntentStatus, ReviewCaseStatus, ReviewResolution
from payment_module.domain.errors import IdempotencyConflict, IntentRejected

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

IMMUTABLE = ("merchant_id", "receiving_account_id", "amount_vnd")


async def stored(app: App, intent_id: uuid.UUID) -> tuple[str, dict[str, object]]:
    """The intent's status and its immutable columns, read back from the table."""
    t = app.tables.payment_intents
    [row] = await app.rows(t, t.c.id == intent_id)
    return row.status, {column: getattr(row, column) for column in IMMUTABLE}


async def test_cancel_keeps_merchant_account_and_amount(app: App) -> None:
    created = await app.intent()
    _, before = await stored(app, created.intent.id)

    await app.module.cancel_intent.execute(app.m1.tenant_id, created.intent.id, "left")

    assert await stored(app, created.intent.id) == (IntentStatus.CANCELLED.value, before)


async def test_supersede_keeps_both_intents_unchanged(app: App) -> None:
    old, new = await app.intent(), await app.intent(amount_vnd=99_000)
    _, old_before = await stored(app, old.intent.id)
    _, new_before = await stored(app, new.intent.id)

    await app.module.cancel_intent.execute(
        app.m1.tenant_id, old.intent.id, "cart changed", superseded_by_intent_id=new.intent.id
    )

    assert await stored(app, old.intent.id) == (IntentStatus.SUPERSEDED.value, old_before)
    assert await stored(app, new.intent.id) == (IntentStatus.AWAITING_PAYMENT.value, new_before)


async def test_expire_keeps_merchant_account_and_amount(app: App) -> None:
    created = await app.intent()
    _, before = await stored(app, created.intent.id)
    app.clock.advance(minutes=20)

    async with app.uow_factory()() as uow:
        assert await uow.intents.set_status(
            app.m1.tenant_id,
            created.intent.id,
            IntentStatus.EXPIRED,
            cancel_reason=None,
            superseded_by_intent_id=None,
        )
        await uow.commit()

    assert await stored(app, created.intent.id) == (IntentStatus.EXPIRED.value, before)


async def test_payment_keeps_merchant_account_and_amount(app: App) -> None:
    created = await app.intent()
    _, before = await stored(app, created.intent.id)

    await app.pay(code=created.payment_reference)

    assert await stored(app, created.intent.id) == (IntentStatus.PAID.value, before)


async def test_late_payment_accepted_in_review_keeps_merchant_account_and_amount(
    app: App,
) -> None:
    created = await app.intent(amount_vnd=120_000)
    _, before = await stored(app, created.intent.id)
    app.clock.advance(minutes=20)
    await app.pay(code=created.payment_reference, amount=120_000)
    cases = app.tables.review_cases
    [case] = await app.rows(cases, cases.c.status == ReviewCaseStatus.OPEN.value)

    await app.module.resolve_review.execute(
        app.m1.tenant_id, case.id, ReviewResolution.ACCEPT_LATE, "ops", note="bank statement"
    )

    assert await stored(app, created.intent.id) == (IntentStatus.PAID.value, before)


@pytest.mark.parametrize(
    ("changed", "refusal"),
    [
        ("amount", IdempotencyConflict),
        # Another merchant's account under the same merchant is refused before the key.
        ("account", IntentRejected),
        ("merchant", IdempotencyConflict),
    ],
)
async def test_refused_replay_keeps_the_stored_request(
    app: App, changed: str, refusal: type[Exception]
) -> None:
    command = app.command()
    created = await app.module.create_intent.execute(command)
    _, before = await stored(app, created.intent.id)
    replay = {
        "amount": {"amount_vnd": 99_000},
        "account": {"receiving_account_id": app.m2.account_id},
        "merchant": {"merchant_id": app.m2.merchant_id, "receiving_account_id": app.m2.account_id},
    }[changed]

    with pytest.raises(refusal):
        await app.module.create_intent.execute(dataclasses.replace(command, **replay))

    assert await stored(app, created.intent.id) == (IntentStatus.AWAITING_PAYMENT.value, before)
    assert await app.count(app.tables.payment_intents) == 1


async def test_intent_expiry_is_reported_without_a_write(app: App) -> None:
    created = await app.intent(expires_at=app.clock.now() + timedelta(minutes=5))
    before = await stored(app, created.intent.id)
    app.clock.advance(minutes=6)

    view = await app.module.get_intent_status.execute(app.m1.tenant_id, created.intent.id)

    assert view.effective_status == IntentStatus.EXPIRED
    assert await stored(app, created.intent.id) == before
