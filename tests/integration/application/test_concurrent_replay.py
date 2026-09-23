"""Acceptance "Concurrent webhook replay": the same delivery 100 times, 20 at a time, with
four workers running concurrently, gives one fact, one settlement and one handler call."""

from __future__ import annotations

import asyncio

import pytest

from fakes.payment_app import App
from payment_module.application.ingest_webhook import IngestStatus

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.acceptance,
    pytest.mark.timeout(600),
]


async def test_one_hundred_concurrent_replays_settle_once(app: App) -> None:
    created = await app.intent()
    raw = app.payment(tx_id=90001, code=created.payment_reference)
    headers = app.headers(raw)
    gate = asyncio.Semaphore(20)
    ingest_done = asyncio.Event()

    async def deliver() -> IngestStatus:
        async with gate:
            result = await app.module.ingest_webhook.execute(app.m1.locator, raw, headers)
            return result.status

    async def worker() -> None:
        while True:
            results = await app.module.process_inbox.run_batch(limit=5)
            if not results and ingest_done.is_set():
                return
            await asyncio.sleep(0.01)

    workers = [asyncio.create_task(worker()) for _ in range(4)]
    statuses = await asyncio.gather(*(deliver() for _ in range(100)))
    ingest_done.set()
    await asyncio.gather(*workers)

    assert statuses.count(IngestStatus.ACCEPTED) == 1
    assert statuses.count(IngestStatus.DUPLICATE) == 99
    assert await app.count(app.tables.webhook_inbox) == 1
    assert await app.count(app.tables.provider_observations) == 1
    assert await app.count(app.tables.provider_transactions) == 1
    assert await app.count(app.tables.settlements) == 1
    assert len(app.handler.calls) == 1
    assert await app.count(app.tables.outbox_events) == 1


async def test_inline_processing_racing_the_worker_settles_once(app: App) -> None:
    for _ in range(10):
        created = await app.intent()
        ingested = await app.webhook(app.payment(code=created.payment_reference))
        await asyncio.gather(
            app.module.process_inbox.process_one(ingested.inbox_id),
            app.module.process_inbox.run_batch(),
            app.build().process_inbox.run_batch(),
        )
    assert await app.count(app.tables.provider_transactions) == 10
    assert await app.count(app.tables.settlements) == 10
    assert len(app.handler.calls) == 10
