"""Case "Durable ACK" over HTTP (invariant 3): no ``{"success": true}`` unless the inbox row
is committed; the provider's retry is then stored exactly once."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fakes.payment_app import App
from payment_module.adapters.sqlalchemy.uow import (
    SqlAlchemyUnitOfWork,
    SqlAlchemyUnitOfWorkFactory,
)

pytestmark = [pytest.mark.integration, pytest.mark.postgres, pytest.mark.acceptance]

INTERNAL = b'{"success":false,"error":"internal"}'


class CommitFails(SqlAlchemyUnitOfWork):
    async def commit(self) -> None:
        await self.session.flush()
        raise ConnectionError("database went away before commit")


async def test_failed_commit_answers_500_and_the_retry_is_stored_once(
    app: App, webhook_client
) -> None:
    broken = app.build(uow_factory=lambda: CommitFails(app.sessions, app.tables))
    raw = app.payment()
    url = f"/webhooks/sepay/{app.m1.locator}"

    failed = await webhook_client(broken).post(url, content=raw, headers=app.headers(raw))

    assert (failed.status_code, failed.content) == (500, INTERNAL)
    assert await app.count(app.tables.webhook_inbox) == 0

    retried = await webhook_client(app.module).post(url, content=raw, headers=app.headers(raw))

    assert (retried.status_code, retried.content) == (200, b'{"success":true}')
    assert await app.count(app.tables.webhook_inbox) == 1


async def test_unreachable_database_answers_500(app: App, webhook_client) -> None:
    dead = create_async_engine(
        "postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none", connect_args={"timeout": 2}
    )
    try:
        module = app.build(
            uow_factory=SqlAlchemyUnitOfWorkFactory(async_sessionmaker(dead), app.tables)
        )
        raw = app.payment()
        response = await webhook_client(module).post(
            f"/webhooks/sepay/{app.m1.locator}", content=raw, headers=app.headers(raw)
        )
    finally:
        await dead.dispose()

    assert (response.status_code, response.content) == (500, INTERNAL)
    assert await app.count(app.tables.webhook_inbox) == 0
