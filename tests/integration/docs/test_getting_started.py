"""The getting-started guide's ``host.py`` blocks, run as one module on PostgreSQL.

The blocks are joined in document order and imported; the test then walks the guide: migrate,
build, onboard, create an intent, post a signed SePay delivery to the app, and see the order
paid by the handler (option A) and by the outbox consumer (option B), each exactly once.
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from payment_module.ports.publisher import OutboxEventView

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

REPO_ROOT = Path(__file__).resolve().parents[3]
GUIDE = REPO_ROOT / "docs" / "getting-started.md"
HOST_BLOCK = re.compile(r'^```python title="host\.py"\n(.*?)^```$', re.MULTILINE | re.DOTALL)
SECRET = "whsec-getting-started"


def host_source(markdown: str) -> str:
    blocks = HOST_BLOCK.findall(markdown)
    assert blocks, "the guide has no host.py blocks"
    return "\n\n".join(blocks)


def load_host(directory: Path) -> ModuleType:
    path = directory / "host.py"
    path.write_text(host_source(GUIDE.read_text(encoding="utf-8")), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("guide_host", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


@pytest.fixture
async def guide_engine(pg_url: str) -> AsyncIterator[AsyncEngine]:
    """An engine on a database of its own: the guide uses the default ``pm_`` prefix."""
    admin = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    database = f"guide_{uuid.uuid4().hex[:8]}"
    async with admin.connect() as conn:
        await conn.execute(sa.text(f'CREATE DATABASE "{database}"'))
    engine = create_async_engine(make_url(pg_url).set(database=database))
    try:
        yield engine
    finally:
        await engine.dispose()
        async with admin.connect() as conn:
            await conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
        await admin.dispose()


async def _eventually(engine: AsyncEngine, query: sa.Select, expected: object) -> object:
    for _ in range(200):
        async with engine.connect() as db:
            found = (await db.execute(query)).all()
        if found == expected:
            return found
        await asyncio.sleep(0.1)
    return found


async def test_the_guide_host_takes_a_payment(
    guide_engine: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(REPO_ROOT / "examples"))
    from _shared.sign_webhook import signed_delivery

    monkeypatch.setenv("SEPAY_WEBHOOK_SECRET", SECRET)
    host = load_host(tmp_path)
    engine = guide_engine

    class RecordingReceipts(host.OrderReceipts):
        def __init__(self, engine: AsyncEngine) -> None:
            super().__init__(engine)
            self.seen: list[OutboxEventView] = []

        async def publish(self, event: OutboxEventView) -> None:
            self.seen.append(event)
            await super().publish(event)

    async with engine.begin() as db:
        await db.run_sync(host.migrate)
    receipts = RecordingReceipts(engine)
    module = host.build(engine, outbox_publisher=receipts)
    connection, account = await host.onboard(module)

    instruction = await host.place_order(module, engine, account, "order-1", 150_000)
    assert instruction.amount.value == 150_000
    assert instruction.payment_reference.startswith("ORD")
    assert instruction.qr_payload is not None and instruction.qr_payload.startswith("000201")

    app = host.create_app(module)
    raw, headers = signed_delivery(
        SECRET, account.account_number, 150_000, instruction.payment_reference, "thanh toan"
    )
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://shop") as http,
    ):
        answer = await http.post(
            f"/webhooks/sepay/{connection.locator}", content=raw, headers=headers
        )
        assert (answer.status_code, answer.json()) == (200, {"success": True})
        paid = sa.select(host.orders.c.status, host.orders.c.last_outcome)
        assert await _eventually(engine, paid, [("paid", "settled")]) == [("paid", "settled")]
        receipt = sa.select(host.payment_receipts.c.order_id)
        assert await _eventually(engine, receipt, [("order-1",)]) == [("order-1",)]

    settled = [event for event in receipts.seen if event.event_type == "PaymentSettled"]
    await receipts.publish(settled[0])  # a redelivery
    async with engine.connect() as db:
        rows = (await db.execute(sa.select(host.payment_receipts))).all()
    assert len(rows) == 1


def test_the_guide_app_factory_reads_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/shop")
    app = load_host(tmp_path).app_from_env()
    assert app.url_path_for("receive_webhook", locator="abc") == "/webhooks/sepay/abc"
