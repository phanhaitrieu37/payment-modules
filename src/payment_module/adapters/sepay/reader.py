"""SePay API v2 transaction reader (``GET /v2/transactions``).

Contract as read in the SePay docs on 22/09/2026, not yet verified on SePay Test:
``Authorization: Bearer <token>``; query ``transaction_date_from``/``transaction_date_to``
(``YYYY-MM-DD HH:mm:ss``, read here as Vietnam time), ``bank_account_id``, ``per_page``
(at most 100), ``page``, ``since_id``; the rows are in ``data``; 3 requests per second per IP,
429 beyond that.

Paging uses the date window and page numbers, never ``since_id``: API ids are UUIDs and
nothing verified says they are ordered in time. The cursor is the next page number of one
fixed window, so a resumed read sees the same window; observations are inserted
idempotently, so re-reading a page is harmless.

Neither the response body nor the ``Authorization`` header is ever logged. A row that cannot
be normalized is skipped, but its id is reported in ``Page.invalid_ids`` so it can be traced.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from payment_module.adapters.sepay.payload import normalize_api_row
from payment_module.adapters.sepay.rate_limiter import ProcessRateLimiter, process_rate_limiter
from payment_module.domain.enums import Environment
from payment_module.domain.errors import DomainError
from payment_module.ports.provider import NormalizedObservation
from payment_module.ports.reader import Page, TransactionReadError, Window
from payment_module.ports.resolvers import ProviderConnection

logger = logging.getLogger(__name__)

LIVE_BASE_URL = "https://userapi.sepay.vn"
TRANSACTIONS_PATH = "/v2/transactions"
MAX_PAGE_SIZE = 100
MAX_INVALID_IDS = 100
MAX_INVALID_ID_CHARS = 64
DEFAULT_RATE_PER_SECOND = 2.0
_VIETNAM = timezone(timedelta(hours=7))
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_RETRYABLE = frozenset({429, 500, 502, 503, 504})
_HTTPS_PORT = 443
# A normalized host: IDNA/ASCII labels or an IP literal; anything else (percent escapes, ...)
# could be read differently by another resolver, so it is refused rather than compared.
_HOST = re.compile(r"[a-z0-9.:-]+")

type Sleep = Callable[[float], Awaitable[None]]


def _format(moment: datetime) -> str:
    return moment.astimezone(_VIETNAM).strftime(_DATE_FORMAT)


def _origin(base_url: str) -> tuple[str, int]:
    """``(host, port)`` of an absolute ``https`` base URL, normalized so every spelling of one
    origin compares equal (case, IDNA and Unicode dots, default port, trailing dots); a URL with
    userinfo or a host that does not normalize to plain ASCII raises ``ValueError``. Hosts are
    not resolved, so an IP literal is only ever equal to itself."""
    try:
        url = httpx.URL(base_url)
    except httpx.InvalidURL as exc:
        raise ValueError("a base URL must be an absolute https URL") from exc
    host = url.host.rstrip(".").lower()
    if url.scheme != "https" or not host:
        raise ValueError("a base URL must be an absolute https URL")
    if url.userinfo:
        raise ValueError("a base URL must not carry credentials")
    if not _HOST.fullmatch(host):
        raise ValueError("a base URL host must be a plain host name or IP address")
    return host, url.port or _HTTPS_PORT


def _invalid_id(row: object) -> str | None:
    """The id of a row that failed normalization, when it is a short string; never the body."""
    raw = row.get("id") if isinstance(row, dict) else None
    if isinstance(raw, str) and 0 < len(raw.strip()) <= MAX_INVALID_ID_CHARS:
        return raw.strip()
    return None


def _page_number(cursor: str | None) -> int:
    if cursor is None:
        return 1
    if not cursor.isascii() or not cursor.isdigit() or int(cursor) < 1:
        raise TransactionReadError("bad_cursor")
    return int(cursor)


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return None


class SePayTransactionReader:
    """One reader per process; the limiter is shared by every reader of the process.

    ``test_base_url`` must be given explicitly to read Test connections, and may never share
    the origin of the configured Live URL nor of the canonical SePay Live URL (a Live proxy
    does not make the real Live origin safe), however it is spelled, so a Test connection can
    never read Live data or send its credential there. Both URLs must be ``https``. Hosts
    usually pass ``page_size=config.reconcile_page_size`` and
    ``rate_per_second=config.reconcile_rate_per_second``; the rate only applies to the first
    limiter created in the process.
    """

    def __init__(
        self,
        *,
        live_base_url: str = LIVE_BASE_URL,
        test_base_url: str | None = None,
        page_size: int = MAX_PAGE_SIZE,
        timeout_s: float = 10.0,
        max_attempts: int = 3,
        rate_per_second: float = DEFAULT_RATE_PER_SECOND,
        limiter: ProcessRateLimiter | None = None,
        client: httpx.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        live_origins = {_origin(live_base_url), _origin(LIVE_BASE_URL)}
        if test_base_url is not None and _origin(test_base_url) in live_origins:
            raise ValueError("the Test base URL must not be the Live base URL")
        if not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"page size must be 1 to {MAX_PAGE_SIZE}")
        self._base_urls: Mapping[Environment, str | None] = {
            Environment.LIVE: live_base_url.rstrip("/"),
            Environment.TEST: None if test_base_url is None else test_base_url.rstrip("/"),
        }
        self._page_size = page_size
        self._timeout = httpx.Timeout(timeout_s)
        self._max_attempts = max(max_attempts, 1)
        self._limiter = limiter or process_rate_limiter(rate_per_second)
        self._client = client
        self._sleep = sleep

    async def list_page(
        self,
        connection: ProviderConnection,
        credential: str,
        cursor: str | None,
        window: Window,
        account_ref: str | None = None,
    ) -> Page:
        base_url = self._base_urls[connection.environment]
        if base_url is None:
            raise TransactionReadError("environment_not_configured")
        page = _page_number(cursor)
        params: dict[str, str | int] = {
            "transaction_date_from": _format(window.start),
            "transaction_date_to": _format(window.end),
            "per_page": self._page_size,
            "page": page,
        }
        if account_ref is not None:
            params["bank_account_id"] = account_ref
        data = await self._get(base_url + TRANSACTIONS_PATH, credential, params, connection)
        rows = data.get("data")
        if not isinstance(rows, list):
            raise TransactionReadError("bad_response")
        observations: list[NormalizedObservation] = []
        delivered: set[str] = set()
        invalid = 0
        invalid_ids: list[str] = []
        for row in rows:
            try:
                if not isinstance(row, dict):
                    raise ValueError("row is not an object")
                observation = normalize_api_row(row)
            except (DomainError, ValueError):
                invalid += 1
                row_id = _invalid_id(row)
                if row_id is not None and len(invalid_ids) < MAX_INVALID_IDS:
                    invalid_ids.append(row_id)
                continue
            observations.append(observation)
            if row.get("webhook_success") in (1, True, "1", "true"):
                delivered.add(observation.source_tx_id)
        logger.info(
            "payment_reconcile_page_read",
            extra={
                "connection_id": str(connection.id),
                "page": page,
                "rows": len(rows),
                "invalid_rows": invalid,
            },
        )
        return Page(
            observations=tuple(observations),
            next_cursor=str(page + 1) if len(rows) >= self._page_size else None,
            webhook_success_ids=frozenset(delivered),
            invalid_rows=invalid,
            invalid_ids=tuple(invalid_ids),
        )

    async def _get(
        self,
        url: str,
        credential: str,
        params: Mapping[str, str | int],
        connection: ProviderConnection,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {credential}", "Accept": "application/json"}
        failure = TransactionReadError("no_attempt")
        for attempt in range(1, self._max_attempts + 1):
            await self._limiter.acquire()
            try:
                response = await self._send(url, headers, params)
            except httpx.HTTPError as exc:
                failure = TransactionReadError(f"transport_{type(exc).__name__}")
                wait = None
            else:
                if response.status_code == 200:
                    try:
                        body = response.json()
                    except ValueError:
                        raise TransactionReadError("bad_response", status=200) from None
                    if not isinstance(body, dict):
                        raise TransactionReadError("bad_response", status=200)
                    return body
                status = response.status_code
                wait = _retry_after(response)
                failure = TransactionReadError(f"http_{status}", status=status, retry_after=wait)
                if status not in _RETRYABLE:
                    raise failure
            logger.warning(
                "payment_reconcile_read_retry",
                extra={
                    "connection_id": str(connection.id),
                    "attempt": attempt,
                    "reason": failure.reason,
                },
            )
            if attempt < self._max_attempts:
                await self._sleep(wait if wait is not None else min(2.0**attempt, 30.0))
        raise failure

    async def _send(
        self, url: str, headers: Mapping[str, str], params: Mapping[str, str | int]
    ) -> httpx.Response:
        if self._client is not None:
            return await self._client.get(
                url, headers=headers, params=params, timeout=self._timeout
            )
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            return await client.get(url, headers=headers, params=params)
