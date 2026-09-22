"""SePay API v2 reader: paging, account filter, retries and the process-wide rate limit.

Uses ``httpx.MockTransport`` and a fake clock; no network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any
from uuid import uuid4

import httpx
import pytest

from payment_module.adapters.sepay.rate_limiter import ProcessRateLimiter
from payment_module.adapters.sepay.reader import LIVE_BASE_URL, SePayTransactionReader
from payment_module.domain.enums import ConnectionStatus, Direction, Environment, ReconcileMode
from payment_module.ports.reader import TransactionReadError, Window
from payment_module.ports.resolvers import ProviderConnection

pytestmark = pytest.mark.contract

TOKEN = "api-token-1"
TEST_URL = "https://sandbox.example.test"
WINDOW = Window(datetime(2026, 9, 21, 2, 0, tzinfo=UTC), datetime(2026, 9, 22, 2, 0, tzinfo=UTC))


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def connection(environment: Environment = Environment.LIVE) -> ProviderConnection:
    return ProviderConnection(
        id=uuid4(),
        tenant_id="t",
        merchant_id=uuid4(),
        environment=environment,
        provider="sepay",
        locator="loc",
        status=ConnectionStatus.ACTIVE,
        reconcile_mode=ReconcileMode.DETECT_ONLY,
        timestamp_tolerance_seconds=300,
        secret_ref="ref",
        api_credential_ref="api-ref",
    )


def row(number: int, **over: Any) -> dict[str, Any]:
    return {
        "id": f"00000000-0000-4000-8000-{number:012d}",
        "bank_account_id": "acc-uuid-1",
        "bank_brand_name": "Vietcombank",
        "account_number": "1017588888",
        "va": None,
        "amount_in": "150000.00",
        "amount_out": "0.00",
        "code": None,
        "transaction_content": f"SUB{number:06d}",
        "reference_number": f"FT{number}",
    } | over


class Api:
    """Serves pages of ``page_size`` rows out of ``rows``; ``script`` forces statuses first."""

    def __init__(self, rows: list[dict[str, Any]], page_size: int, script: list[int] = ()) -> None:
        self.rows = rows
        self.page_size = page_size
        self.script = list(script)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.script:
            status = self.script.pop(0)
            return httpx.Response(status, headers={"Retry-After": "1"}, json={"error": "x"})
        page = int(request.url.params["page"])
        start = (page - 1) * self.page_size
        return httpx.Response(200, json={"data": self.rows[start : start + self.page_size]})


def reader(api: Api, time: FakeTime, **over: Any) -> SePayTransactionReader:
    limiter = ProcessRateLimiter(2.0, monotonic=time.monotonic, sleep=time.sleep)
    options: dict[str, Any] = {
        "test_base_url": TEST_URL,
        "page_size": api.page_size,
        "limiter": limiter,
        "client": httpx.AsyncClient(transport=httpx.MockTransport(api)),
        "sleep": time.sleep,
    } | over
    return SePayTransactionReader(**options)


async def read_all(r: SePayTransactionReader, conn: ProviderConnection) -> list[Any]:
    pages, cursor = [], None
    while True:
        page = await r.list_page(conn, TOKEN, cursor, WINDOW, account_ref="acc-uuid-1")
        pages.append(page)
        cursor = page.next_cursor
        if cursor is None:
            return pages


async def test_three_pages_then_the_window_is_exhausted() -> None:
    api, time = Api([row(n) for n in range(1, 8)], page_size=3), FakeTime()
    pages = await read_all(reader(api, time), connection())

    assert [len(page.observations) for page in pages] == [3, 3, 1]
    assert [page.next_cursor for page in pages] == ["2", "3", None]
    first = pages[0].observations[0]
    assert first.source_tx_id == "00000000-0000-4000-8000-000000000001"
    assert first.direction == Direction.IN
    assert first.amount.value == 150_000
    assert first.bank_reference == "FT1"
    request = api.requests[0]
    assert request.url.host == "userapi.sepay.vn"
    assert request.url.path == "/v2/transactions"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    params = request.url.params
    assert params["bank_account_id"] == "acc-uuid-1"
    assert params["per_page"] == "3"
    assert params["page"] == "1"
    assert "since_id" not in params
    # Window sent in Vietnam time.
    assert params["transaction_date_from"] == "2026-09-21 09:00:00"
    assert params["transaction_date_to"] == "2026-09-22 09:00:00"


async def test_requests_never_exceed_the_configured_rate() -> None:
    api, time = Api([row(n) for n in range(1, 11)], page_size=1), FakeTime()
    r = reader(api, time)
    sent: list[float] = []
    original = api.__call__

    def record(request: httpx.Request) -> httpx.Response:
        sent.append(time.now)
        return original(request)

    r._client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    await read_all(r, connection())

    assert len(sent) == 11
    gaps = [later - earlier for earlier, later in pairwise(sent)]
    assert min(gaps) >= 0.5 - 1e-9  # 2 requests per second


async def test_throttled_request_is_retried_and_the_cursor_does_not_move() -> None:
    api, time = Api([row(1), row(2)], page_size=1, script=[429, 503]), FakeTime()
    page = await reader(api, time, max_attempts=3).list_page(
        connection(), TOKEN, "2", WINDOW, account_ref="acc-uuid-1"
    )
    assert [obs.bank_reference for obs in page.observations] == ["FT2"]
    assert [request.url.params["page"] for request in api.requests] == ["2", "2", "2"]
    assert 1.0 in time.sleeps  # Retry-After honoured


async def test_persistent_throttling_raises_without_a_page() -> None:
    api, time = Api([row(1)], page_size=1, script=[429, 429, 429]), FakeTime()
    with pytest.raises(TransactionReadError) as caught:
        await reader(api, time, max_attempts=3).list_page(connection(), TOKEN, None, WINDOW)
    assert caught.value.status == 429
    assert caught.value.retry_after == 1.0
    assert len(api.requests) == 3


async def test_client_error_is_not_retried() -> None:
    api, time = Api([row(1)], page_size=1, script=[401]), FakeTime()
    with pytest.raises(TransactionReadError) as caught:
        await reader(api, time).list_page(connection(), TOKEN, None, WINDOW)
    assert caught.value.reason == "http_401"
    assert len(api.requests) == 1


async def test_transport_error_is_retried() -> None:
    time, calls = FakeTime(), []

    def flaky(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectTimeout("timeout", request=request)
        return httpx.Response(200, json={"data": [row(1)]})

    r = reader(Api([], 1), time, client=httpx.AsyncClient(transport=httpx.MockTransport(flaky)))
    page = await r.list_page(connection(), TOKEN, None, WINDOW)
    assert len(page.observations) == 1
    assert len(calls) == 2


@pytest.mark.parametrize("body", [b"not json", b"[]", json.dumps({"rows": []}).encode()])
async def test_unexpected_body_raises(body: bytes) -> None:
    time = FakeTime()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    r = reader(Api([], 1), time, client=httpx.AsyncClient(transport=transport))
    with pytest.raises(TransactionReadError):
        await r.list_page(connection(), TOKEN, None, WINDOW)


async def test_invalid_rows_are_counted_skipped_and_named() -> None:
    rows = [row(1), row(2, amount_in="1.5"), "junk", row(3, webhook_success=1)]
    api, time = Api(rows, page_size=10), FakeTime()
    page = await reader(api, time).list_page(connection(), TOKEN, None, WINDOW)
    assert [obs.bank_reference for obs in page.observations] == ["FT1", "FT3"]
    assert page.invalid_rows == 2
    assert page.invalid_ids == (row(2)["id"],)
    assert page.webhook_success_ids == {row(3)["id"]}
    assert page.next_cursor is None


async def test_amount_beyond_bigint_is_an_invalid_row() -> None:
    rows = [row(1, amount_in=str(2**63)), row(2, amount_in=str(2**63 - 1))]
    api, time = Api(rows, page_size=10), FakeTime()
    page = await reader(api, time).list_page(connection(), TOKEN, None, WINDOW)
    assert [obs.amount.value for obs in page.observations] == [2**63 - 1]
    assert page.invalid_ids == (row(1)["id"],)


async def test_invalid_ids_never_carry_unbounded_or_non_string_ids() -> None:
    rows = [row(1, id="x" * 65, amount_in="bad"), row(2, id=7, amount_in="bad")]
    rows += [row(n, amount_in="bad") for n in range(3, 3 + 105)]
    api, time = Api(rows, page_size=200), FakeTime()  # a provider ignoring per_page
    page = await reader(api, time, page_size=100).list_page(connection(), TOKEN, None, WINDOW)
    assert page.invalid_rows == 107
    assert len(page.invalid_ids) == 100
    assert page.invalid_ids[0] == row(3)["id"]


async def test_test_connection_uses_the_test_base_url() -> None:
    api, time = Api([], page_size=1), FakeTime()
    await reader(api, time).list_page(connection(Environment.TEST), TOKEN, None, WINDOW)
    assert str(api.requests[0].url).startswith(TEST_URL + "/v2/transactions")


async def test_test_connection_is_refused_without_a_test_base_url() -> None:
    api, time = Api([], page_size=1), FakeTime()
    with pytest.raises(TransactionReadError) as caught:
        await reader(api, time, test_base_url=None).list_page(
            connection(Environment.TEST), TOKEN, None, WINDOW
        )
    assert caught.value.reason == "environment_not_configured"
    assert api.requests == []


@pytest.mark.parametrize(
    "test_url",
    [
        LIVE_BASE_URL + "/",
        "HTTPS://USERAPI.SEPAY.VN:443/",
        "https://userapi.sepay.vn./",
        "https://UserApi.Sepay.vn/other/path",
    ],
)
def test_test_base_url_may_not_share_the_live_origin(test_url: str) -> None:
    with pytest.raises(ValueError):
        SePayTransactionReader(test_base_url=test_url)


@pytest.mark.parametrize(
    "options",
    [
        {"test_base_url": "http://sandbox.example.test"},
        {"live_base_url": "http://userapi.sepay.vn"},
        {"test_base_url": "sandbox.example.test"},
        {"test_base_url": "https://"},
    ],
)
def test_base_urls_must_be_absolute_https(options: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        SePayTransactionReader(**options)


def test_another_port_on_the_live_host_is_another_origin() -> None:
    SePayTransactionReader(test_base_url="https://userapi.sepay.vn:8443")


@pytest.mark.parametrize("cursor", ["0", "abc", "-1"])
async def test_bad_cursor_is_refused(cursor: str) -> None:
    api, time = Api([], page_size=1), FakeTime()
    with pytest.raises(TransactionReadError):
        await reader(api, time).list_page(connection(), TOKEN, cursor, WINDOW)


async def test_limiter_spaces_bursts() -> None:
    time = FakeTime()
    limiter = ProcessRateLimiter(2.0, monotonic=time.monotonic, sleep=time.sleep)
    stamps = []
    for _ in range(5):
        await limiter.acquire()
        stamps.append(time.now)
    assert stamps == [0.0, 0.5, 1.0, 1.5, 2.0]
    time.now += timedelta(seconds=10).total_seconds()
    await limiter.acquire()
    assert time.sleeps == [0.5, 0.5, 0.5, 0.5]


def test_one_limiter_per_process() -> None:
    from payment_module.adapters.sepay.rate_limiter import process_rate_limiter

    first = process_rate_limiter(2.0)
    assert process_rate_limiter(5.0) is first
    assert SePayTransactionReader()._limiter is first
