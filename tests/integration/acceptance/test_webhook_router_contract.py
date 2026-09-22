"""HTTP contract of the webhook router (sepay-integration.md ACK rules, invariants 1 and 3).

Cases "Merchant isolation" (404 for unknown and disabled look the same) and "Durable ACK".
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
import time
from collections.abc import AsyncIterator
from datetime import timedelta
from uuid import UUID

import pytest

from fakes.fake_provider import body
from fakes.payment_app import App, Scope
from payment_module.adapters.fastapi import make_webhook_router
from payment_module.application.config import PaymentModuleConfig
from payment_module.domain.enums import ConnectionStatus, InboxStatus
from payment_module.ports.handlers import SettlementView
from payment_module.ports.resolvers import ProviderConnection
from payment_module.ports.unit_of_work import UnitOfWork

pytestmark = [pytest.mark.integration, pytest.mark.postgres, pytest.mark.acceptance]

OK = b'{"success":true}'
NOT_FOUND = b'{"success":false,"error":"not_found"}'
UNAUTHORIZED = b'{"success":false,"error":"unauthorized"}'
TOO_LARGE = b'{"success":false,"error":"payload_too_large"}'
INTERNAL = b'{"success":false,"error":"internal"}'


def url(scope: Scope | str) -> str:
    return f"/webhooks/sepay/{scope if isinstance(scope, str) else scope.locator}"


async def post(client, app: App, raw: bytes, scope: Scope | str | None = None, **headers):
    return await client.post(url(scope or app.m1), content=raw, headers=headers or app.headers(raw))


async def test_unknown_and_disabled_locators_answer_the_same_404(app: App, webhook_client) -> None:
    client = webhook_client(app.module)
    raw = app.payment()
    unknown = await post(client, app, raw, "no-such-locator")
    await app.set_connection_status(app.m2, ConnectionStatus.DISABLED)
    raw_m2 = app.payment(app.m2)
    disabled = await post(client, app, raw_m2, app.m2)

    assert (unknown.status_code, disabled.status_code) == (404, 404)
    assert unknown.content == disabled.content == NOT_FOUND
    assert dict(unknown.headers) == dict(disabled.headers)
    assert await app.count(app.tables.webhook_inbox) == 0
    assert app.metrics.counts == {"webhook_unknown_locator_total": 2}


@pytest.mark.parametrize("failure", ["wrong_secret", "stale", "missing"])
async def test_signature_or_timestamp_failure_is_401_and_stores_nothing(
    app: App, webhook_client, failure: str
) -> None:
    raw = app.payment()
    headers = {
        "wrong_secret": app.headers(raw, secret="other"),
        "stale": app.headers(raw, at=app.clock.now() - timedelta(hours=1)),
        "missing": {"content-type": "application/json"},
    }[failure]

    response = await webhook_client(app.module).post(url(app.m1), content=raw, headers=headers)

    assert (response.status_code, response.content) == (401, UNAUTHORIZED)
    assert await app.count(app.tables.webhook_inbox) == 0
    assert app.metrics.counts == {"webhook_auth_failures_total": 1}


@pytest.mark.parametrize("chunked", [False, True])
async def test_body_over_the_limit_is_413_and_never_stored(
    app: App, webhook_client, chunked: bool
) -> None:
    module = app.build(config=PaymentModuleConfig(worker_owner="w", max_body_bytes=256))
    raw = app.payment(content="x" * 300)

    async def pieces() -> AsyncIterator[bytes]:
        for start in range(0, len(raw), 64):
            yield raw[start : start + 64]

    client = webhook_client(module)
    content = pieces() if chunked else raw
    response = await client.post(url(app.m1), content=content, headers=app.headers(raw))

    assert (response.status_code, response.content) == (413, TOO_LARGE)
    assert await app.count(app.tables.webhook_inbox) == 0
    assert app.metrics.counts == {"webhook_body_too_large_total": 1}


async def test_body_over_the_limit_is_413_before_the_locator_is_looked_up(
    app: App, webhook_client
) -> None:
    module = app.build(config=PaymentModuleConfig(worker_owner="w", max_body_bytes=256))
    raw = app.payment(content="x" * 300)

    response = await post(webhook_client(module), app, raw, "no-such-locator")

    assert response.status_code == 413
    assert "webhook_unknown_locator_total" not in app.metrics.counts


async def test_body_exactly_at_the_limit_is_accepted(app: App, webhook_client) -> None:
    raw = app.payment()
    module = app.build(config=PaymentModuleConfig(worker_owner="w", max_body_bytes=len(raw)))

    response = await post(webhook_client(module), app, raw)

    assert (response.status_code, response.content) == (200, OK)


class ExplodingSecrets:
    async def webhook_secrets(self, connection: ProviderConnection) -> list[str]:
        raise ConnectionError("vault unreachable")

    async def api_credential(self, connection: ProviderConnection) -> str | None:
        return None


async def test_unexpected_failure_is_500_and_logs_no_body(
    app: App, webhook_client, caplog: pytest.LogCaptureFixture
) -> None:
    module = app.build(secret_resolver=ExplodingSecrets())
    raw = app.payment(content="NGUYEN VAN A chuyen tien")
    caplog.set_level(logging.INFO)

    response = await post(webhook_client(module), app, raw)

    assert (response.status_code, response.content) == (500, INTERNAL)
    [record] = [r for r in caplog.records if r.getMessage() == "payment_webhook_failed"]
    assert record.body_sha256 == hashlib.sha256(raw).hexdigest()[:12]
    assert record.locator_sha256 != app.m1.locator
    ours = [r for r in caplog.records if r.name.startswith("payment_module")]
    logged = " ".join(f"{r.getMessage()} {vars(r)}" for r in ours)
    assert "NGUYEN VAN A" not in logged
    assert app.m1.locator not in logged
    assert await app.count(app.tables.webhook_inbox) == 0


async def test_every_stored_delivery_is_acknowledged(app: App, webhook_client) -> None:
    client = webhook_client(app.module)
    raw = app.payment()
    no_event_id = body(None, account=app.m1.account_number)

    answers = [await post(client, app, r) for r in (raw, raw, no_event_id)]

    assert [(a.status_code, a.content) for a in answers] == [(200, OK)] * 3
    statuses = sorted(row.status for row in await app.rows(app.tables.webhook_inbox))
    assert statuses == [InboxStatus.QUARANTINED.value, InboxStatus.RECEIVED.value]


async def test_without_a_budget_the_answer_does_not_process(app: App, webhook_client) -> None:
    intent = await app.intent()
    raw = app.payment(code=intent.intent.payment_reference)

    assert (await post(webhook_client(app.module), app, raw)).status_code == 200

    assert await app.count(app.tables.settlements) == 0


async def test_inline_processing_settles_before_the_answer(app: App, webhook_client) -> None:
    intent = await app.intent()
    raw = app.payment(code=intent.intent.payment_reference)
    client = webhook_client(app.module, process_inline_budget_s=5)

    assert (await post(client, app, raw)).content == OK

    assert await app.count(app.tables.settlements) == 1
    assert len(app.handler.calls) == 1


class SlowHandler:
    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.calls = 0

    async def on_settled(self, uow: UnitOfWork, settled: SettlementView) -> None:
        await asyncio.sleep(self.delay)
        self.calls += 1


async def test_inline_processing_past_the_budget_is_answered_and_not_cancelled(
    app: App, webhook_client
) -> None:
    slow = SlowHandler(0.5)
    module = app.build(settlement_handler=slow)
    intent = await app.intent()
    raw = app.payment(code=intent.intent.payment_reference)
    client = webhook_client(module, process_inline_budget_s=0.05)

    started = time.monotonic()
    response = await post(client, app, raw)
    answered_after = time.monotonic() - started

    assert (response.status_code, answered_after < 0.45) == (200, True)
    for _ in range(100):
        if await app.count(app.tables.settlements):
            break
        await asyncio.sleep(0.05)
    assert (await app.count(app.tables.settlements), slow.calls) == (1, 1)


class SpyInbox:
    def __init__(self, inner) -> None:
        self.inner = inner
        self.calls: list[UUID] = []

    async def process_one(self, inbox_id: UUID):
        self.calls.append(inbox_id)
        return await self.inner.process_one(inbox_id)


async def test_only_a_newly_accepted_delivery_is_processed_inline(app: App, webhook_client) -> None:
    spy = SpyInbox(app.module.process_inbox)
    client = webhook_client(
        dataclasses.replace(app.module, process_inbox=spy), process_inline_budget_s=5
    )
    raw = app.payment()

    for delivery in (raw, raw, body(None, account=app.m1.account_number)):
        assert (await post(client, app, delivery)).status_code == 200

    assert len(spy.calls) == 1


async def test_inline_failure_still_acknowledges_and_leaves_a_retry(
    app: App, webhook_client
) -> None:
    app.handler.failures = 1
    intent = await app.intent()
    raw = app.payment(code=intent.intent.payment_reference)

    response = await post(webhook_client(app.module, process_inline_budget_s=5), app, raw)

    assert (response.status_code, response.content) == (200, OK)
    [row] = await app.rows(app.tables.webhook_inbox)
    assert row.status == InboxStatus.RETRY_WAIT.value
    assert await app.count(app.tables.settlements) == 0


async def test_a_path_without_locator_is_refused(app: App) -> None:
    with pytest.raises(ValueError):
        make_webhook_router(app.module, path="/webhooks/sepay")
