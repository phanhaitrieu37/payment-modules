"""Example host, option B: no web framework; the bill is paid by the outbox consumer.

Cases "Reuse" and "Host async failure" (validation-and-acceptance.md). Tables come from one
``MetaData`` with ``create_all`` (the dev path). Smoke with DATABASE_URL (asyncpg) and
SEPAY_WEBHOOK_SECRET: ``PYTHONPATH=examples python -m fnb_host.main``; prints JSON.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from datetime import timedelta
from importlib.metadata import version

import sqlalchemy as sa
from _shared.sign_webhook import signed_delivery
from fnb_host.consumer import BillConsumer, bill_receipts, metadata, table_bills
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from payment_module.adapters.sepay.checklist import SePayTemplateChecklist
from payment_module.adapters.sepay.provider import SePayProvider
from payment_module.adapters.sqlalchemy.tables import define_tables
from payment_module.adapters.sqlalchemy.uow import SqlAlchemyUnitOfWorkFactory
from payment_module.application.config import PaymentModuleConfig
from payment_module.application.create_intent import CreateIntentCommand, CreateIntentResult
from payment_module.builder import PaymentModule, build_payment_module
from payment_module.domain.enums import ConnectionStatus, Environment
from payment_module.ports.clock import Clock, SystemClock
from payment_module.ports.publisher import OutboxPublisher
from payment_module.runtime import PaymentWorker, WorkerIntervals
from payment_module.secrets import EnvSecretResolver

TENANT, ADMIN, ACCOUNT, TEST = "fnb", "admin@fnb", "0071000654321", Environment.TEST
TABLES = define_tables(metadata)  # the host's own MetaData: create_all sees every table


def build(engine: AsyncEngine, publisher: OutboxPublisher, clock: Clock | None = None):
    return build_payment_module(
        PaymentModuleConfig(),
        SqlAlchemyUnitOfWorkFactory(async_sessionmaker(engine, expire_on_commit=False), TABLES),
        {"sepay": SePayProvider()},
        EnvSecretResolver(),
        clock or SystemClock(),
        outbox_publisher=publisher,
        template_checklists={"sepay": SePayTemplateChecklist()},
    )


async def onboard(module: PaymentModule) -> tuple:
    """Merchant, account, connection and profile v1, in the order readiness requires."""
    merchant = await module.register_merchant.execute(TENANT, "branch-1", ADMIN)
    account = await module.register_receiving_account.execute(
        TENANT, merchant.id, TEST, "VCB", ACCOUNT, "CONG TY FNB", ADMIN
    )
    conn = await module.register_connection.execute(
        TENANT, merchant.id, "sepay", TEST, "env:SEPAY_WEBHOOK_SECRET", ADMIN
    )
    await module.bind_connection_account.execute(TENANT, conn.id, account.id, ADMIN)
    draft = await module.create_reference_profile.execute(1, {"bill": "BILL"}, 6, "23456789", ADMIN)
    done = {i.key: True for i in SePayTemplateChecklist().checklist(conn, draft.profile).items}
    await module.record_connection_readiness.execute(TENANT, conn.id, 1, TEST, done, "t1", ADMIN)
    await module.activate_reference_profile.execute(1, ADMIN)
    await module.set_connection_status.execute(
        TENANT, conn.id, ConnectionStatus.ACTIVE, ADMIN, "ok"
    )
    return conn, account.id


async def open_bill(
    module: PaymentModule, engine: AsyncEngine, bill_id: str, amount_vnd: int, **scope
) -> CreateIntentResult:
    async with engine.begin() as db:
        await db.execute(sa.insert(table_bills).values(id=bill_id, amount_vnd=amount_vnd))
    command = CreateIntentCommand(
        TENANT,
        amount_vnd=amount_vnd,
        host_ref_type="table_bill",
        host_ref_id=bill_id,
        idempotency_key=bill_id,
        expires_at=module.clock.now() + timedelta(minutes=30),
        prefix_name="bill",
        **scope,
    )
    return await module.create_intent.execute(command)


async def smoke() -> dict[str, object]:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as db:
        await db.run_sync(metadata.create_all)
    module = build(engine, BillConsumer(engine))
    conn, account_id = await onboard(module)
    bill = await open_bill(
        module,
        engine,
        "table-7",
        320_000,
        merchant_id=conn.merchant_id,
        receiving_account_id=account_id,
    )
    before = await module.get_intent_status.execute(TENANT, bill.intent.id)
    code = bill.intent.payment_reference
    raw, headers = signed_delivery(os.environ["SEPAY_WEBHOOK_SECRET"], ACCOUNT, 320_000, code, code)
    ingested = await module.ingest_webhook.execute(conn.locator, raw, headers)

    stop = asyncio.Event()
    worker = asyncio.create_task(PaymentWorker(module, WorkerIntervals(0.05, 0.05)).run(stop))
    for _ in range(400):
        async with engine.connect() as db:
            receipts = (await db.execute(sa.select(bill_receipts))).all()
        if receipts:
            break
        await asyncio.sleep(0.05)
    stop.set()
    await worker
    after = await module.get_intent_status.execute(TENANT, bill.intent.id)
    async with engine.connect() as db:
        paid_by = (await db.execute(sa.select(table_bills.c.receipt_event_id))).scalar_one()
    await engine.dispose()
    event = json.loads(receipts[0].message)
    return {
        "package_version": version("payment-module"),
        "fastapi_installed": importlib.util.find_spec("fastapi") is not None,
        "intent_status_before_payment": before.effective_status.value,
        "ingest_status": ingested.status.value,
        "intent_status_after_payment": after.effective_status.value,
        "receipts": len(receipts),
        "bill_paid_by_event": str(paid_by) == event["event_id"],
        "event": {key: event[key] for key in ("event_type", "schema_version", "amount_vnd")},
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(smoke())))
