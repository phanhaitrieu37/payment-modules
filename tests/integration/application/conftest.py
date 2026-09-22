"""Fixtures for application tests; the helpers live in ``fakes.payment_app``."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from fakes.fake_provider import FakeProvider, StaticSecretResolver
from fakes.payment_app import (
    SECRET,
    App,
    FakeClock,
    RecordingHandler,
    RecordingMetrics,
    RecordingObserver,
    RecordingPublisher,
    seed_world,
)
from payment_module.adapters.sqlalchemy.tables import define_tables
from payment_module.adapters.sqlalchemy.uow import SqlAlchemyUnitOfWorkFactory
from payment_module.application.config import PaymentModuleConfig
from payment_module.builder import build_payment_module


@pytest.fixture
async def app(pg_url: str) -> AsyncIterator[App]:
    engine = create_async_engine(pg_url, pool_size=20, max_overflow=10)
    metadata = sa.MetaData()
    tables = define_tables(metadata, prefix=f"t{uuid.uuid4().hex[:8]}_")
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        seeded = await seed_world(conn, tables)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    clock, provider = FakeClock(), FakeProvider()
    secrets = StaticSecretResolver([SECRET])
    handler, observer = RecordingHandler(), RecordingObserver()
    metrics, publisher = RecordingMetrics(), RecordingPublisher()
    module = build_payment_module(
        PaymentModuleConfig(worker_owner="worker-test"),
        SqlAlchemyUnitOfWorkFactory(sessions, tables),
        {"fake": provider},
        secrets,
        clock,
        settlement_handler=handler,
        outcome_observer=observer,
        metrics=metrics,
        outbox_publisher=publisher,
    )
    try:
        yield App(
            engine=engine,
            sessions=sessions,
            tables=tables,
            clock=clock,
            provider=provider,
            secrets=secrets,
            handler=handler,
            observer=observer,
            metrics=metrics,
            publisher=publisher,
            module=module,
            **seeded,
        )
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(metadata.drop_all)
        await engine.dispose()
