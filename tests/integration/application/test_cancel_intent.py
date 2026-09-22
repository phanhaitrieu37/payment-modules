"""CancelIntent: cancelled or superseded, only from awaiting_payment, joined host rollback."""

from __future__ import annotations

import uuid

import pytest

from fakes.payment_app import App
from payment_module.adapters.sqlalchemy.uow import SqlAlchemyUnitOfWork
from payment_module.domain.enums import IntentStatus
from payment_module.domain.errors import IllegalTransition, IntentNotFound, IntentRejected

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


async def stored(app: App, intent_id: uuid.UUID):
    t = app.tables.payment_intents
    [row] = await app.rows(t, t.c.id == intent_id)
    return row


async def test_cancel_without_successor_is_cancelled(app: App) -> None:
    created = await app.intent()
    view = await app.module.cancel_intent.execute(app.m1.tenant_id, created.intent.id, "left")

    assert view.status == IntentStatus.CANCELLED
    row = await stored(app, created.intent.id)
    assert (row.status, row.cancel_reason, row.superseded_by_intent_id) == (
        IntentStatus.CANCELLED.value,
        "left",
        None,
    )


async def test_cancel_with_successor_is_superseded(app: App) -> None:
    old, new = await app.intent(), await app.intent(amount_vnd=99_000)
    view = await app.module.cancel_intent.execute(
        app.m1.tenant_id, old.intent.id, "cart changed", superseded_by_intent_id=new.intent.id
    )

    assert view.status == IntentStatus.SUPERSEDED
    assert view.superseded_by_intent_id == new.intent.id
    row = await stored(app, old.intent.id)
    assert (row.status, row.superseded_by_intent_id) == (
        IntentStatus.SUPERSEDED.value,
        new.intent.id,
    )


async def test_paid_intent_cannot_be_cancelled(app: App) -> None:
    created = await app.intent()
    await app.pay(code=created.payment_reference)
    with pytest.raises(IllegalTransition):
        await app.module.cancel_intent.execute(app.m1.tenant_id, created.intent.id, "late")
    assert (await stored(app, created.intent.id)).status == IntentStatus.PAID.value


async def test_cancelled_intent_cannot_be_cancelled_again(app: App) -> None:
    created = await app.intent()
    await app.module.cancel_intent.execute(app.m1.tenant_id, created.intent.id, "first")
    with pytest.raises(IllegalTransition):
        await app.module.cancel_intent.execute(app.m1.tenant_id, created.intent.id, "second")


async def test_unknown_or_foreign_intent_is_not_found(app: App) -> None:
    created = await app.intent()
    with pytest.raises(IntentNotFound):
        await app.module.cancel_intent.execute(app.m1.tenant_id, uuid.uuid4(), "x")
    with pytest.raises(IntentNotFound):
        await app.module.cancel_intent.execute(app.mb.tenant_id, created.intent.id, "x")


@pytest.mark.parametrize("successor", ["other-merchant", "other-environment", "self", "unknown"])
async def test_successor_must_share_merchant_and_environment(app: App, successor: str) -> None:
    created = await app.intent()
    successor_id = {
        "other-merchant": lambda: app.intent(app.m2),
        "other-environment": lambda: app.intent(app.m1_live),
    }
    if successor in successor_id:
        target = (await successor_id[successor]()).intent.id
    else:
        target = created.intent.id if successor == "self" else uuid.uuid4()
    with pytest.raises(IntentRejected) as caught:
        await app.module.cancel_intent.execute(
            app.m1.tenant_id, created.intent.id, "x", superseded_by_intent_id=target
        )
    assert caught.value.code == "INVALID_SUPERSEDING_INTENT"
    assert (await stored(app, created.intent.id)).status == IntentStatus.AWAITING_PAYMENT.value


class _HostFailed(Exception):
    pass


async def test_joined_cancel_follows_the_host_transaction(app: App) -> None:
    created = await app.intent()
    with pytest.raises(_HostFailed):
        async with app.sessions() as session, session.begin():
            uow = SqlAlchemyUnitOfWork.joined(session, app.tables)
            await app.module.cancel_intent.execute(
                app.m1.tenant_id, created.intent.id, "x", uow=uow
            )
            raise _HostFailed
    assert (await stored(app, created.intent.id)).status == IntentStatus.AWAITING_PAYMENT.value

    async with app.sessions() as session, session.begin():
        uow = SqlAlchemyUnitOfWork.joined(session, app.tables)
        await app.module.cancel_intent.execute(app.m1.tenant_id, created.intent.id, "x", uow=uow)
    assert (await stored(app, created.intent.id)).status == IntentStatus.CANCELLED.value


async def test_cancel_racing_a_payment_is_serialized(app: App) -> None:
    """Cancel first, then the payment: the payment goes to review, never settles."""
    created = await app.intent()
    raw = app.payment(code=created.payment_reference)
    await app.webhook(raw)
    await app.module.cancel_intent.execute(app.m1.tenant_id, created.intent.id, "x")
    await app.module.process_inbox.run_batch()

    assert await app.count(app.tables.settlements) == 0
    [case] = await app.rows(app.tables.review_cases)
    assert case.reason == "INTENT_CANCELLED"
