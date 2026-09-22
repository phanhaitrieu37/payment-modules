"""Durable webhook intake: verify, store in the inbox, commit, and only then acknowledge.

Only the provider event id is read here; every other field is trusted only after the worker
normalizes the stored body. The HTTP router maps the outcomes:

* :class:`ConnectionNotFound` (unknown locator or disabled connection) -> 404, one message;
* :class:`WebhookAuthError` (signature or timestamp) -> 401, nothing stored;
* :class:`PayloadTooLarge` -> 413, nothing stored;
* any other exception (a database failure before commit) -> 5xx, no acknowledgement;
* every :class:`IngestResult`, quarantine included -> 200.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from payment_module.application import provider_for
from payment_module.application.config import PaymentModuleConfig
from payment_module.domain.enums import ConnectionStatus, InboxStatus, ProcessingErrorCode
from payment_module.domain.errors import ConnectionNotFound, PayloadTooLarge, WebhookAuthError
from payment_module.ports.clock import Clock
from payment_module.ports.metrics import MetricsSink, increment_safely
from payment_module.ports.provider import EventKey, PaymentProvider
from payment_module.ports.resolvers import SecretResolver
from payment_module.ports.unit_of_work import UnitOfWorkFactory

logger = logging.getLogger(__name__)

NO_EVENT_KEY = ProcessingErrorCode.NO_EVENT_KEY


class IngestStatus(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    QUARANTINED = "quarantined"


@dataclass(frozen=True, slots=True)
class IngestResult:
    """``inbox_id`` is the stored row; for a duplicate it is the row stored first."""

    status: IngestStatus
    inbox_id: UUID


class IngestWebhook:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        providers: Mapping[str, PaymentProvider],
        secret_resolver: SecretResolver,
        clock: Clock,
        metrics: MetricsSink,
        config: PaymentModuleConfig,
    ) -> None:
        self._uow_factory = uow_factory
        self._providers = providers
        self._secrets = secret_resolver
        self._clock = clock
        self._metrics = metrics
        self._config = config

    async def execute(
        self,
        locator: str,
        raw_body: bytes,
        headers: Mapping[str, str],
        now: datetime | None = None,
    ) -> IngestResult:
        now = now or self._clock.now()
        async with self._uow_factory() as uow:
            connection = await uow.connections.by_locator(locator)
        # Pending and not-ready connections still ingest: the money is real; their status only
        # gates new intents.
        if connection is None or connection.status == ConnectionStatus.DISABLED:
            increment_safely(self._metrics, "webhook_unknown_locator_total")
            raise ConnectionNotFound()
        if len(raw_body) > self._config.max_body_bytes:
            raise PayloadTooLarge(f"body exceeds {self._config.max_body_bytes} bytes")

        # Secret lookup and verification may be slow (a vault call); no transaction is open.
        provider = provider_for(self._providers, connection.provider)
        secrets = await self._secrets.webhook_secrets(connection)
        try:
            verified = provider.verify(
                raw_body, headers, secrets, now, connection.timestamp_tolerance_seconds
            )
        except WebhookAuthError as exc:
            increment_safely(
                self._metrics,
                "webhook_auth_failures_total",
                {"connection_id": str(connection.id), "code": exc.code},
            )
            logger.warning(
                "payment_webhook_rejected",
                extra={"connection_id": str(connection.id), "code": exc.code},
            )
            raise

        event_key = provider.extract_event_key(verified)
        status = InboxStatus.RECEIVED
        if event_key is None:
            event_key = EventKey.body_hash(raw_body)
            status = InboxStatus.QUARANTINED
        body_sha256 = hashlib.sha256(raw_body).hexdigest()
        allowed = {name.lower() for name in self._config.header_allowlist}
        stored_headers = {k.lower(): v for k, v in headers.items() if k.lower() in allowed}

        async with self._uow_factory() as uow:
            inbox_id = await uow.inbox.add(
                tenant_id=connection.tenant_id,
                connection_id=connection.id,
                event_key=event_key.value,
                event_key_kind=event_key.kind,
                body_sha256=body_sha256,
                raw_body=raw_body,
                headers=stored_headers,
                received_at=now,
                status=status,
                last_error_code=NO_EVENT_KEY.value if status == InboxStatus.QUARANTINED else None,
                purge_after=self._config.purge_after(now),
            )
            duplicate = inbox_id is None
            if inbox_id is None:
                inbox_id = await uow.inbox.find_id(connection.id, event_key.value)
                assert inbox_id is not None
            await uow.commit()

        result_status = (
            IngestStatus.DUPLICATE
            if duplicate
            else IngestStatus.QUARANTINED
            if status == InboxStatus.QUARANTINED
            else IngestStatus.ACCEPTED
        )
        log_fields = {
            "connection_id": str(connection.id),
            "inbox_id": str(inbox_id),
            "body_sha256": body_sha256[:12],
            "status": result_status.value,
        }
        if result_status == IngestStatus.QUARANTINED:
            increment_safely(
                self._metrics,
                "inbox_quarantined_total",
                {"connection_id": str(connection.id), "reason": NO_EVENT_KEY.value},
            )
            logger.warning("payment_webhook_quarantined", extra=log_fields)
        else:
            logger.info("payment_webhook_ingested", extra=log_fields)
        return IngestResult(result_status, inbox_id)
