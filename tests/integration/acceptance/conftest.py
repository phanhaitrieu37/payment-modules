"""Acceptance fixtures: fresh databases, the example hosts on ``sys.path``, HTTP clients.

The cases are listed in ``validation-and-acceptance.md``; ``traceability.py`` maps each of
them and each architecture invariant to the tests that prove it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from payment_module.adapters.fastapi import make_webhook_router
from payment_module.builder import PaymentModule

REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLES = REPO_ROOT / "examples"

CreateDatabase = Callable[[str], Awaitable[str]]


@pytest.fixture
def examples_on_path(monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.syspath_prepend(str(EXAMPLES))
    return EXAMPLES


@pytest.fixture
async def create_database(pg_url: str) -> AsyncIterator[CreateDatabase]:
    """``await create_database("saas")`` makes an empty database named ``saas_<8 hex>`` and
    returns its async URL; every database made is dropped after the test."""
    admin = create_async_engine(pg_url, isolation_level="AUTOCOMMIT")
    created: list[str] = []

    async def create(name: str) -> str:
        database = f"{name}_{uuid.uuid4().hex[:8]}"
        async with admin.connect() as conn:
            await conn.execute(sa.text(f'CREATE DATABASE "{database}"'))
        created.append(database)
        return make_url(pg_url).set(database=database).render_as_string(hide_password=False)

    try:
        yield create
    finally:
        async with admin.connect() as conn:
            for database in created:
                await conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
        await admin.dispose()


WebhookClient = Callable[..., httpx.AsyncClient]


@pytest.fixture
async def webhook_client() -> AsyncIterator[WebhookClient]:
    """``webhook_client(module, **router_options)``: a client for the webhook router alone.

    A failing app answers 500 instead of raising into the test, as a real server would.
    """
    clients: list[httpx.AsyncClient] = []

    def make(module: PaymentModule, **router: Any) -> httpx.AsyncClient:
        app = FastAPI()
        app.include_router(make_webhook_router(module, **router))
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        clients.append(httpx.AsyncClient(transport=transport, base_url="http://host"))
        return clients[-1]

    try:
        yield make
    finally:
        for client in clients:
            await client.aclose()
