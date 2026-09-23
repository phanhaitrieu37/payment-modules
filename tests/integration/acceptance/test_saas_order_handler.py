"""Option A example handler: the order is paid in the settlement transaction, and a
settlement for an order the host does not have rolls back and is retried."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from fakes.payment_app import App
from payment_module.domain.enums import InboxStatus, IntentStatus

pytestmark = [pytest.mark.integration, pytest.mark.postgres, pytest.mark.acceptance]


@pytest.fixture
async def orders(app: App, examples_on_path: Path) -> AsyncIterator[sa.Table]:
    from saas_host.handler import orders

    async with app.engine.begin() as db:
        await db.run_sync(orders.metadata.create_all, tables=[orders])
    try:
        yield orders
    finally:
        async with app.engine.begin() as db:
            await db.run_sync(orders.metadata.drop_all, tables=[orders])


def saas_module(app: App) -> Any:
    from saas_host.handler import MarkOrderPaid

    return app.build(settlement_handler=MarkOrderPaid())


async def pay(app: App, module: Any, code: str) -> None:
    raw = app.payment(code=code, content=f"{code} thanh toan")
    await module.ingest_webhook.execute(app.m1.locator, raw, app.headers(raw))
    await module.process_inbox.run_batch()


async def test_settlement_pays_the_order_in_the_same_transaction(
    app: App, orders: sa.Table
) -> None:
    module = saas_module(app)
    await app.execute(sa.insert(orders).values(id="order-1", amount_vnd=150_000, status="new"))
    created = await module.create_intent.execute(app.command(host_ref_id="order-1"))

    await pay(app, module, created.payment_reference)

    [order] = await app.rows(orders)
    assert order.status == "paid"
    assert await app.count(app.tables.settlements) == 1


async def test_settlement_for_a_missing_order_rolls_back_and_is_retried(
    app: App, orders: sa.Table
) -> None:
    module = saas_module(app)
    created = await module.create_intent.execute(app.command(host_ref_id="order-2"))

    await pay(app, module, created.payment_reference)

    inbox = app.tables.webhook_inbox
    [delivery] = await app.rows(inbox)
    assert delivery.status == InboxStatus.RETRY_WAIT.value
    assert await app.count(app.tables.settlements) == 0
    status = await module.get_intent_status.execute(app.m1.tenant_id, created.intent.id)
    assert status.effective_status == IntentStatus.AWAITING_PAYMENT

    await app.execute(sa.insert(orders).values(id="order-2", amount_vnd=150_000, status="new"))
    app.clock.advance(seconds=5)  # past the first retry delay
    await module.process_inbox.run_batch()

    [order] = await app.rows(orders)
    assert order.status == "paid"
    assert await app.count(app.tables.settlements) == 1
    assert (await app.rows(inbox))[0].status == InboxStatus.PROCESSED.value
