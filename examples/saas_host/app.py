"""Example host, option A: FastAPI webhook, the order is paid in the settlement transaction.

Cases "Reuse", "Create idempotency", "Rotation" (validation-and-acceptance.md). Smoke with
DATABASE_URL, SEPAY_WEBHOOK_SECRET, EVIDENCE_ROOT: ``PYTHONPATH=examples python -m saas_host.app``
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import timedelta
from importlib.metadata import version
from pathlib import Path

import httpx
import sqlalchemy as sa
from _shared.sign_webhook import signed_delivery
from fastapi import FastAPI
from saas_host.alembic_env import migrate
from saas_host.handler import MarkOrderPaid, RecordLastOutcome, orders
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from payment_module.adapters.evidence_file import FileEvidenceVerifier
from payment_module.adapters.fastapi import make_webhook_router
from payment_module.adapters.sepay.checklist import SePayTemplateChecklist
from payment_module.adapters.sepay.provider import SePayProvider
from payment_module.adapters.sqlalchemy.tables import define_tables
from payment_module.adapters.sqlalchemy.uow import SqlAlchemyUnitOfWork, SqlAlchemyUnitOfWorkFactory
from payment_module.application.config import PaymentModuleConfig
from payment_module.application.create_intent import CreateIntentCommand
from payment_module.builder import PaymentModule, build_payment_module
from payment_module.domain.enums import ConnectionStatus, Environment, ReconcileMode
from payment_module.ports.clock import SystemClock
from payment_module.secrets import EnvSecretResolver

TENANT, ADMIN, ACCOUNT, LEGACY_CODE = "saas", "admin@saas", "0071000123456", "OLDSHOP-1001"
EVIDENCE = "sepay-test-verification.example.json"  # a file under EVIDENCE_ROOT
TABLES, TEST = define_tables(sa.MetaData()), Environment.TEST


def build(engine: AsyncEngine) -> PaymentModule:
    return build_payment_module(
        PaymentModuleConfig(pii_retention_days=30),
        SqlAlchemyUnitOfWorkFactory(async_sessionmaker(engine, expire_on_commit=False), TABLES),
        {"sepay": SePayProvider()},
        EnvSecretResolver(),
        SystemClock(),
        settlement_handler=MarkOrderPaid(),
        outcome_observer=RecordLastOutcome(),
        evidence_verifier=FileEvidenceVerifier(Path(os.environ["EVIDENCE_ROOT"]), SystemClock()),
        template_checklists={"sepay": SePayTemplateChecklist()},
    )


def create_app(module: PaymentModule) -> FastAPI:
    app = FastAPI()
    app.include_router(make_webhook_router(module, process_inline_budget_s=2))
    return app


async def onboard(module: PaymentModule) -> tuple:
    """Merchant, account, connection and profile v1, in the order readiness requires."""
    merchant = await module.register_merchant.execute(TENANT, "shop-1", ADMIN)
    account = await module.register_receiving_account.execute(
        TENANT, merchant.id, TEST, "VCB", ACCOUNT, "CONG TY SAAS", ADMIN
    )
    conn = await module.register_connection.execute(
        TENANT, merchant.id, "sepay", TEST, "env:SEPAY_WEBHOOK_SECRET", ADMIN
    )
    await module.bind_connection_account.execute(TENANT, conn.id, account.id, ADMIN)
    prefixes = {"subscription": "SUB", "topup": "TOP"}
    draft = await module.create_reference_profile.execute(1, prefixes, 8, "ABCDEFGHJKLMNP", ADMIN)
    done = {i.key: True for i in SePayTemplateChecklist().checklist(conn, draft.profile).items}
    await module.record_connection_readiness.execute(TENANT, conn.id, 1, TEST, done, "t1", ADMIN)
    await module.activate_reference_profile.execute(1, ADMIN)
    await module.set_connection_status.execute(
        TENANT, conn.id, ConnectionStatus.ACTIVE, ADMIN, "ok"
    )
    return conn, account.id


async def place_order(module: PaymentModule, engine: AsyncEngine, order: dict, **intent):
    """Insert the order and create its intent in one host transaction."""
    async with async_sessionmaker(engine)() as session, session.begin():
        new_order = insert(orders).values(status="new", **order).on_conflict_do_nothing()
        await session.execute(new_order)
        ref = {"host_ref_type": "order", "host_ref_id": order["id"], "idempotency_key": order["id"]}
        command = CreateIntentCommand(TENANT, amount_vnd=order["amount_vnd"], **ref, **intent)
        created = await module.create_intent.execute(
            command, SqlAlchemyUnitOfWork.joined(session, TABLES)
        )
        mark = sa.update(orders).where(orders.c.id == order["id"])
        await session.execute(mark.values(intent_id=created.intent.id))
    return created


async def smoke() -> dict[str, object]:
    engine = create_async_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as db:
        await db.run_sync(migrate)
    module = build(engine)
    conn, account_id = await onboard(module)
    scope = {"merchant_id": conn.merchant_id, "receiving_account_id": account_id}
    scope["expires_at"] = module.clock.now() + timedelta(minutes=15)
    http = httpx.AsyncClient(transport=httpx.ASGITransport(create_app(module)), base_url="http://h")

    async def pay(code: str | None, content: str, amount_vnd: int) -> int:
        secret = os.environ["SEPAY_WEBHOOK_SECRET"]
        raw, headers = signed_delivery(secret, ACCOUNT, amount_vnd, code, content)
        url = f"/webhooks/sepay/{conn.locator}"
        status = (await http.post(url, content=raw, headers=headers)).status_code
        await module.process_inbox.run_batch()  # the worker's share if inline ran out of budget
        return status

    order = {"id": "order-1", "amount_vnd": 150_000}
    first = await place_order(module, engine, order, prefix_name="subscription", **scope)
    replay = await place_order(module, engine, order, prefix_name="subscription", **scope)
    before = await module.get_intent_status.execute(TENANT, first.intent.id)
    code = first.intent.payment_reference
    webhook_status = await pay(code, f"{code} thanh toan", 150_000)

    # Rotation: an old-system code is paid before the host imports it, then rematched.
    await pay(None, f"{LEGACY_CODE} thanh toan", 99_000)
    await module.import_legacy_reference_profile.execute(100, ADMIN)
    legacy = {"id": "order-legacy", "amount_vnd": 99_000, "order_code": LEGACY_CODE}
    legacy_intent = {"reference_override": LEGACY_CODE, "reference_profile_version": 100}
    await place_order(module, engine, legacy, **legacy_intent, **scope)
    rematched = await module.rematch_reviews.execute(TENANT, ADMIN)

    set_mode = module.set_reconcile_mode.execute  # auto_settle only with valid evidence
    auto = await set_mode(TENANT, conn.id, ReconcileMode.AUTO_SETTLE, EVIDENCE, ADMIN)
    back = await set_mode(TENANT, conn.id, ReconcileMode.DETECT_ONLY, None, ADMIN)
    async with engine.connect() as db:
        paid, legacy_paid = (await db.execute(sa.select(orders).order_by(orders.c.id))).all()
    await http.aclose()
    await engine.dispose()
    return {
        "package_version": version("payment-module"),
        "intent_status_before_payment": before.effective_status.value,
        "replay_same_intent": replay.intent.id == first.intent.id and not replay.created,
        "webhook_status": webhook_status,
        "order": {"status": paid.status, "last_outcome": paid.last_outcome},
        "legacy_order_status": legacy_paid.status,
        "rematched": [result.match_state.value for result in rematched if result.match_state],
        "reconcile_modes": [auto.reconcile_mode.value, back.reconcile_mode.value],
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(smoke())))
