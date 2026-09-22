"""Thin HTTP edge for :class:`~payment_module.application.ingest_webhook.IngestWebhook`.

Answers, each with a fixed JSON body so nothing about the connection leaks:

* 200 ``{"success": true}`` for every stored delivery, duplicates and quarantine included,
  and only after the inbox row is committed;
* 404 ``not_found`` for an unknown locator and a disabled connection alike;
* 401 ``unauthorized`` for a bad signature or timestamp;
* 413 ``payload_too_large`` when the body exceeds ``config.max_body_bytes``; the stream is
  read with that cap, so an oversized body is never buffered whole. A ``Content-Length``
  that is not a plain ASCII decimal is ignored and the counted stream decides;
* 400 ``client_disconnected`` when the client goes away before the body is complete
  (nobody reads it; nothing is stored or logged as a failure);
* 500 ``internal`` for anything else, reading the body included (typically the database),
  so the provider retries.

Logs carry only ``body_sha256[:12]`` and a hash of the locator, never the body or signature.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from uuid import UUID

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from starlette.requests import ClientDisconnect

from payment_module.application.ingest_webhook import IngestStatus
from payment_module.builder import PaymentModule
from payment_module.domain.errors import ConnectionNotFound, PayloadTooLarge, WebhookAuthError
from payment_module.ports.metrics import increment_safely

logger = logging.getLogger(__name__)

BODY_TOO_LARGE_METRIC = "webhook_body_too_large_total"

_ASCII_DECIMAL = re.compile(r"[0-9]+")
# More significant digits than this is beyond any body cap, and never parsed.
_MAX_LENGTH_DIGITS = 18


class _BodyTooLarge(Exception):
    pass


def _answer(status_code: int, error: str | None = None) -> JSONResponse:
    if error is None:
        return JSONResponse({"success": True}, status_code=status_code)
    return JSONResponse({"success": False, "error": error}, status_code=status_code)


def _short_hash(value: bytes | str) -> str:
    data = value.encode() if isinstance(value, str) else value
    return hashlib.sha256(data).hexdigest()[:12]


def _declares_too_much(declared: str | None, limit: int) -> bool:
    """``True`` when a well-formed ``Content-Length`` exceeds ``limit``; anything else is
    left to the counted stream."""
    if declared is None or _ASCII_DECIMAL.fullmatch(declared) is None:
        return False
    digits = declared.lstrip("0")
    return len(digits) > _MAX_LENGTH_DIGITS or int(digits or "0") > limit


async def _read_capped(request: Request, limit: int) -> bytes:
    if _declares_too_much(request.headers.get("content-length"), limit):
        raise _BodyTooLarge
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise _BodyTooLarge
        chunks.append(chunk)
    return b"".join(chunks)


def make_webhook_router(
    module: PaymentModule,
    *,
    path: str = "/webhooks/sepay/{locator}",
    process_inline_budget_s: float = 0.0,
) -> APIRouter:
    """``path`` must contain ``{locator}``.

    With ``process_inline_budget_s > 0`` a newly accepted delivery is also processed right
    after its commit, and the answer waits up to that many seconds for it. The answer is 200
    either way: processing that runs longer is not cancelled (it holds a lease) and the
    worker covers any failure.
    """
    if "{locator}" not in path:
        raise ValueError("path must contain {locator}")
    router = APIRouter()
    inline_tasks: set[asyncio.Task[object]] = set()

    def _inline_done(task: asyncio.Task[object]) -> None:
        inline_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning(
                "payment_inline_processing_failed",
                extra={"error": type(task.exception()).__name__},
            )

    async def _process_inline(inbox_id: UUID) -> None:
        task: asyncio.Task[object] = asyncio.create_task(module.process_inbox.process_one(inbox_id))
        inline_tasks.add(task)
        task.add_done_callback(_inline_done)
        await asyncio.wait({task}, timeout=process_inline_budget_s)

    @router.post(path, include_in_schema=False)
    async def receive_webhook(locator: str, request: Request) -> Response:
        try:
            raw_body = await _read_capped(request, module.config.max_body_bytes)
        except _BodyTooLarge:
            increment_safely(module.metrics, BODY_TOO_LARGE_METRIC)
            return _answer(413, "payload_too_large")
        except ClientDisconnect:
            logger.info(
                "payment_webhook_client_disconnected",
                extra={"locator_sha256": _short_hash(locator)},
            )
            return _answer(400, "client_disconnected")
        except Exception as exc:
            logger.error(
                "payment_webhook_body_read_failed",
                extra={"locator_sha256": _short_hash(locator), "error": type(exc).__name__},
            )
            return _answer(500, "internal")
        try:
            result = await module.ingest_webhook.execute(locator, raw_body, dict(request.headers))
        except ConnectionNotFound:
            return _answer(404, "not_found")
        except WebhookAuthError:
            return _answer(401, "unauthorized")
        except PayloadTooLarge:
            increment_safely(module.metrics, BODY_TOO_LARGE_METRIC)
            return _answer(413, "payload_too_large")
        except Exception as exc:
            logger.error(
                "payment_webhook_failed",
                extra={
                    "locator_sha256": _short_hash(locator),
                    "body_sha256": _short_hash(raw_body),
                    "error": type(exc).__name__,
                },
            )
            return _answer(500, "internal")
        if result.status == IngestStatus.ACCEPTED and process_inline_budget_s > 0:
            await _process_inline(result.inbox_id)
        return _answer(200)

    return router
