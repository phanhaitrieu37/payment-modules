"""Webhook intake: 401/404/413 semantics, durable ACK, event keys and quarantine."""

from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
import sqlalchemy as sa

from fakes.fake_provider import body
from fakes.payment_app import SECRET, App
from payment_module.adapters.sepay.provider import SePayProvider, sign
from payment_module.adapters.sqlalchemy.uow import SqlAlchemyUnitOfWork
from payment_module.application.config import PaymentModuleConfig
from payment_module.application.ingest_webhook import IngestStatus
from payment_module.domain.enums import (
    ConnectionStatus,
    EventKeyKind,
    InboxStatus,
    ProcessingErrorCode,
)
from payment_module.domain.errors import ConnectionNotFound, PayloadTooLarge, WebhookAuthError

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


async def test_valid_delivery_is_accepted_and_stored(app: App) -> None:
    raw = app.payment(tx_id=501)
    result = await app.webhook(raw)

    assert result.status == IngestStatus.ACCEPTED
    [row] = await app.rows(app.tables.webhook_inbox)
    assert row.id == result.inbox_id
    assert row.event_key == "webhook:501"
    assert row.event_key_kind == EventKeyKind.PROVIDER_ID.value
    assert row.status == InboxStatus.RECEIVED.value
    assert row.body_sha256 == hashlib.sha256(raw).hexdigest()
    assert bytes(row.raw_body) == raw
    assert row.received_at == app.clock.now()
    assert row.connection_id == app.m1.connection_id


async def test_only_allowlisted_headers_are_stored(app: App) -> None:
    raw = app.payment()
    headers = app.headers(raw) | {"Authorization": "Apikey secret", "X-Forwarded-For": "1.2.3.4"}
    await app.module.ingest_webhook.execute(app.m1.locator, raw, headers)

    [row] = await app.rows(app.tables.webhook_inbox)
    assert set(row.headers) == {"content-type", "x-sepay-timestamp"}


async def test_redelivery_is_a_duplicate_of_the_first_row(app: App) -> None:
    raw = app.payment(tx_id=502)
    first = await app.webhook(raw)
    again = await app.webhook(raw)

    assert again.status == IngestStatus.DUPLICATE
    assert again.inbox_id == first.inbox_id
    assert await app.count(app.tables.webhook_inbox) == 1


@pytest.mark.parametrize(
    "raw",
    [b"not json at all", body(None), body("12ab"), body(True), b"[1, 2]"],
    ids=["not-json", "missing-id", "non-digit-id", "boolean-id", "not-an-object"],
)
async def test_verified_body_without_usable_id_is_quarantined(app: App, raw: bytes) -> None:
    result = await app.webhook(raw)

    assert result.status == IngestStatus.QUARANTINED
    [row] = await app.rows(app.tables.webhook_inbox)
    assert row.status == InboxStatus.QUARANTINED.value
    assert row.event_key == f"sha256:{hashlib.sha256(raw).hexdigest()}"
    assert row.event_key_kind == EventKeyKind.BODY_HASH.value
    assert row.last_error_code == "no_event_key"
    assert app.metrics.counts["inbox_quarantined_total"] == 1

    again = await app.webhook(raw)
    assert again.status == IngestStatus.DUPLICATE
    assert again.inbox_id == result.inbox_id


async def test_wrong_signature_is_rejected_and_not_stored(app: App) -> None:
    raw = app.payment()
    with pytest.raises(WebhookAuthError) as caught:
        await app.module.ingest_webhook.execute(
            app.m1.locator, raw, app.headers(raw, secret="another-secret")
        )
    assert caught.value.code == WebhookAuthError.INVALID_SIGNATURE
    assert await app.count(app.tables.webhook_inbox) == 0
    assert app.metrics.counts["webhook_auth_failures_total"] == 1


async def test_tampered_body_is_rejected(app: App) -> None:
    raw = app.payment(amount=150_000)
    headers = app.headers(raw)
    tampered = raw.replace(b"150000", b"990000")
    with pytest.raises(WebhookAuthError):
        await app.module.ingest_webhook.execute(app.m1.locator, tampered, headers)
    assert await app.count(app.tables.webhook_inbox) == 0


@pytest.mark.parametrize("missing", ["X-Signature", "X-Sepay-Timestamp"])
async def test_missing_auth_header_is_rejected(app: App, missing: str) -> None:
    raw = app.payment()
    headers = {k: v for k, v in app.headers(raw).items() if k != missing}
    with pytest.raises(WebhookAuthError) as caught:
        await app.module.ingest_webhook.execute(app.m1.locator, raw, headers)
    assert caught.value.code == WebhookAuthError.MISSING_HEADER
    assert await app.count(app.tables.webhook_inbox) == 0


@pytest.mark.parametrize("skew", [timedelta(seconds=-301), timedelta(seconds=301)])
async def test_timestamp_outside_default_tolerance_is_rejected(app: App, skew: timedelta) -> None:
    raw = app.payment()
    headers = app.headers(raw, at=app.clock.now() + skew)
    with pytest.raises(WebhookAuthError) as caught:
        await app.module.ingest_webhook.execute(app.m1.locator, raw, headers)
    assert caught.value.code == WebhookAuthError.STALE_TIMESTAMP
    assert await app.count(app.tables.webhook_inbox) == 0


async def test_timestamp_inside_tolerance_is_accepted(app: App) -> None:
    raw = app.payment()
    headers = app.headers(raw, at=app.clock.now() - timedelta(seconds=299))
    result = await app.module.ingest_webhook.execute(app.m1.locator, raw, headers)
    assert result.status == IngestStatus.ACCEPTED


async def test_connection_tolerance_is_used(app: App) -> None:
    t = app.tables.provider_connections
    await app.execute(
        sa.update(t).where(t.c.id == app.m1.connection_id).values(timestamp_tolerance_seconds=60)
    )
    raw = app.payment()
    headers = app.headers(raw, at=app.clock.now() - timedelta(seconds=120))
    with pytest.raises(WebhookAuthError):
        await app.module.ingest_webhook.execute(app.m1.locator, raw, headers)


async def test_any_secret_of_the_rotation_window_verifies(app: App) -> None:
    app.secrets.secrets = ["whsec-new", SECRET]
    result = await app.webhook(app.payment())
    assert result.status == IngestStatus.ACCEPTED


async def test_unknown_and_disabled_locators_look_the_same(app: App) -> None:
    raw = app.payment()
    with pytest.raises(ConnectionNotFound) as unknown:
        await app.module.ingest_webhook.execute("f" * 32, raw, app.headers(raw))
    await app.set_connection_status(app.m1, ConnectionStatus.DISABLED)
    with pytest.raises(ConnectionNotFound) as disabled:
        await app.webhook(raw)

    assert str(unknown.value) == str(disabled.value)
    assert type(unknown.value) is type(disabled.value)
    assert await app.count(app.tables.webhook_inbox) == 0
    assert app.secrets.calls == 0 and app.provider.verify_calls == 0
    assert app.metrics.counts["webhook_unknown_locator_total"] == 2


@pytest.mark.parametrize("status", [ConnectionStatus.PENDING, ConnectionStatus.NOT_READY])
async def test_pending_and_not_ready_connections_still_ingest(
    app: App, status: ConnectionStatus
) -> None:
    await app.set_connection_status(app.m1, status)
    result = await app.webhook(app.payment())
    assert result.status == IngestStatus.ACCEPTED


async def test_oversized_body_is_rejected_before_verification(app: App) -> None:
    module = app.build(config=PaymentModuleConfig(worker_owner="w", max_body_bytes=64))
    raw = app.payment(content="x" * 100)
    with pytest.raises(PayloadTooLarge):
        await module.ingest_webhook.execute(app.m1.locator, raw, app.headers(raw))
    assert app.provider.verify_calls == 0
    assert await app.count(app.tables.webhook_inbox) == 0


async def test_retention_sets_purge_after(app: App) -> None:
    module = app.build(config=PaymentModuleConfig(worker_owner="w", pii_retention_days=30))
    raw = app.payment()
    await module.ingest_webhook.execute(app.m1.locator, raw, app.headers(raw))
    [row] = await app.rows(app.tables.webhook_inbox)
    assert row.purge_after == app.clock.now() + timedelta(days=30)


class _CommitFails(SqlAlchemyUnitOfWork):
    async def commit(self) -> None:
        await self.session.flush()
        raise ConnectionError("database went away before commit")


async def test_no_ack_without_commit(app: App) -> None:
    module = app.build(uow_factory=lambda: _CommitFails(app.sessions, app.tables))
    raw = app.payment()
    with pytest.raises(ConnectionError):
        await module.ingest_webhook.execute(app.m1.locator, raw, app.headers(raw))
    assert await app.count(app.tables.webhook_inbox) == 0

    # The provider retries; with the database back the delivery is stored once.
    assert (await app.webhook(raw)).status == IngestStatus.ACCEPTED
    assert await app.count(app.tables.webhook_inbox) == 1


def sepay_body(app: App, tx_id: str, amount: str) -> bytes:
    """A SePay webhook body; ``tx_id`` and ``amount`` are raw JSON number literals."""
    return (
        f'{{"id": {tx_id}, "gateway": "VCB", "accountNumber": "{app.m1.account_number}", '
        f'"subAccount": null, "transferType": "in", "transferAmount": {amount}, '
        f'"code": null, "content": "pay", "referenceCode": "FT1"}}'
    ).encode()


async def sepay_webhook(app: App, raw: bytes):
    module = app.build(provider_registry={"fake": SePayProvider()})
    timestamp = int(app.clock.now().timestamp())
    headers = {
        "Content-Type": "application/json",
        "X-SePay-Timestamp": str(timestamp),
        "X-SePay-Signature": sign(raw, SECRET, timestamp),
    }
    return module, await module.ingest_webhook.execute(app.m1.locator, raw, headers)


async def test_sepay_numeric_id_beyond_the_digit_cap_is_quarantined_by_body_hash(
    app: App,
) -> None:
    raw = sepay_body(app, "9" * 300, "150000")
    _, result = await sepay_webhook(app, raw)

    assert result.status == IngestStatus.QUARANTINED
    [row] = await app.rows(app.tables.webhook_inbox)
    assert row.event_key == f"sha256:{hashlib.sha256(raw).hexdigest()}"
    assert row.last_error_code == "no_event_key"


async def test_sepay_amount_beyond_bigint_is_quarantined_by_the_worker(app: App) -> None:
    module, result = await sepay_webhook(app, sepay_body(app, "777001", str(2**63)))
    assert result.status == IngestStatus.ACCEPTED

    await module.process_inbox.run_batch()

    [row] = await app.rows(app.tables.webhook_inbox)
    assert row.status == InboxStatus.QUARANTINED.value
    assert row.last_error_code == ProcessingErrorCode.NORMALIZE_FAILED.value
    assert await app.count(app.tables.provider_transactions) == 0
