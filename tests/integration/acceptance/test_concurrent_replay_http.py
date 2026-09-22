"""Case "Concurrent webhook replay" over HTTP: the same signed delivery posted 100 times at
once through the router, with inline processing and four workers racing, is acknowledged
every time and gives one fact, one settlement and one fulfilment (invariants 3 and 4)."""

from __future__ import annotations

import asyncio

import pytest

from fakes.payment_app import App

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.acceptance,
    pytest.mark.timeout(300),
]


@pytest.mark.parametrize("inline_budget", [0.0, 1.0])
async def test_one_hundred_concurrent_posts_settle_once(
    app: App, webhook_client, inline_budget: float
) -> None:
    created = await app.intent()
    raw = app.payment(tx_id=91001, code=created.intent.payment_reference)
    headers = app.headers(raw)
    client = webhook_client(app.module, process_inline_budget_s=inline_budget)
    posted = asyncio.Event()

    async def worker() -> None:
        while True:
            results = await app.module.process_inbox.run_batch(limit=5)
            if not results and posted.is_set():
                return
            await asyncio.sleep(0.01)

    workers = [asyncio.create_task(worker()) for _ in range(4)]
    url = f"/webhooks/sepay/{app.m1.locator}"
    answers = await asyncio.gather(
        *(client.post(url, content=raw, headers=headers) for _ in range(100))
    )
    posted.set()
    await asyncio.gather(*workers)

    assert {(a.status_code, a.content) for a in answers} == {(200, b'{"success":true}')}
    assert await app.count(app.tables.webhook_inbox) == 1
    assert await app.count(app.tables.provider_observations) == 1
    assert await app.count(app.tables.provider_transactions) == 1
    assert await app.count(app.tables.settlements) == 1
    assert await app.count(app.tables.outbox_events) == 1
    assert len(app.handler.calls) == 1
