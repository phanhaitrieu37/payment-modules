# Getting started

**English** · [Tiếng Việt](getting-started.vi.md)

This guide takes a new host project from nothing to a paid order: install `payment-module`,
create its tables, wire it, onboard one merchant, receive a signed SePay webhook and mark the
order paid. It is written for developers and for coding agents.

The code blocks titled `host.py`, read top to bottom, form one working host module.
[`tests/integration/docs/test_getting_started.py`](../tests/integration/docs/test_getting_started.py)
extracts them and runs them on PostgreSQL, so they match the released code. For larger hosts,
see [`examples/saas_host`](../examples/saas_host) (FastAPI, settlement in the host
transaction, legacy code import) and [`examples/fnb_host`](../examples/fnb_host) (no web
framework, outbox consumer).

What the module owns and what your host owns: the module confirms, stores and reconciles money
that lands in the merchant's own bank account. Your host owns prices, orders, taxes and
entitlements, and calls the module with the final amount to pay.

## 0. Prerequisites

- Python 3.12 or newer.
- PostgreSQL. The schema uses PostgreSQL features (partial unique indexes, `JSONB`,
  `SKIP LOCKED`); the async driver is `asyncpg`.
- A SePay account with the receiving bank account linked, and access to its webhook and
  payment-code settings. Test and Live are separate environments; start with Test.

## 1. Install

Install the wheel from the GitHub Release, with the extras your host needs:

```sh
uv pip install "payment-module[sqlalchemy,postgres,fastapi,sepay] @ https://github.com/phanhaitrieu37/payment-modules/releases/download/v0.1.0/payment_module-0.1.0-py3-none-any.whl"
```

Or pin the git tag:

```sh
uv pip install "payment-module[sqlalchemy,postgres,fastapi,sepay] @ git+https://github.com/phanhaitrieu37/payment-modules@v0.1.0"
```

The release page lists a `SHA256SUMS` file; compare it with `shasum -a 256` on the wheel you
downloaded. Extras: `sqlalchemy` (tables, repositories, unit of work, Alembic), `postgres`
(asyncpg), `fastapi` (webhook router, leave it out if you have no FastAPI app), `sepay`
(provider, API reader, VietQR). The four versioned contracts (public API, event schema,
normalization, DB schema) and the 0.x policy are in [CHANGELOG.md](../CHANGELOG.md).

## 2. Create the schema

The module's tables are defined by
[`define_tables`](../src/payment_module/adapters/sqlalchemy/tables.py) (every name prefixed
`pm_`) and created by the frozen migration
[`schema_v1.upgrade`](../src/payment_module/adapters/sqlalchemy/migrations/schema_v1.py).
Nothing runs on import: your host runs the migration, after a backup. In an Alembic revision,
call `schema_v1.upgrade(op)` with Alembic's `op`; the function below does the same outside
Alembic, then creates the host's own tables.

```python title="host.py"
"""A minimal host: one shop, one bank account, orders paid by bank transfer."""

from __future__ import annotations

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from payment_module.adapters.sqlalchemy.migrations import schema_v1
from payment_module.adapters.sqlalchemy.tables import define_tables

TABLES = define_tables(sa.MetaData())  # the pm_* tables, created only by schema_v1

metadata = sa.MetaData()  # the host's own tables
orders = sa.Table(
    "orders",
    metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("amount_vnd", sa.BigInteger(), nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("intent_id", sa.Uuid()),
    sa.Column("last_outcome", sa.String(32)),
)


def migrate(sync_conn: sa.Connection) -> None:
    """What the host's migration runs: the frozen payment schema, then the host tables."""
    schema_v1.upgrade(Operations(MigrationContext.configure(sync_conn)))
    metadata.create_all(sync_conn)
```

A later schema version ships as a new module with an upgrade step; `schema_v1` never changes.

## 3. Secrets and configuration

- **Webhook secret.** A connection stores only a reference such as `env:SEPAY_WEBHOOK_SECRET`.
  [`EnvSecretResolver`](../src/payment_module/secrets/env_secret_resolver.py) reads that
  variable, and also `SEPAY_WEBHOOK_SECRET_PREVIOUS` while you rotate the secret. Implement the
  [`SecretResolver`](../src/payment_module/ports/resolvers.py) port to read from your own
  secret store instead.
- **Module settings.** [`PaymentModuleConfig`](../src/payment_module/application/config.py)
  has a safe default for every field; `build_payment_module` calls its `validate()` and
  refuses settings that would lose money evidence. The field you most likely set is
  `pii_retention_days` (see [Operations](#10-operations)).
- **Environment and tenant.** `tenant_id` is your host's isolation key; it comes from your
  authenticated context, never from the payer. Every account and connection belongs to one
  environment, `test` or `live`.

```python title="host.py"
TENANT = "demo-shop"  # from the host's authenticated context in a real app
ADMIN = "admin@demo-shop.example"  # the operator recorded on onboarding steps
WEBHOOK_SECRET_REF = "env:SEPAY_WEBHOOK_SECRET"
```

## 4. Build the module

[`build_payment_module`](../src/payment_module/builder.py) wires every use case once and
returns a `PaymentModule`; keep one per process. It needs a unit-of-work factory over your
engine, the provider registry, a secret resolver and a clock. The optional arguments decide
how your host hears about payments (step 9).

```python title="host.py"
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from payment_module.adapters.sepay.checklist import SePayTemplateChecklist
from payment_module.adapters.sepay.provider import SePayProvider
from payment_module.adapters.sqlalchemy.uow import SqlAlchemyUnitOfWorkFactory
from payment_module.application.config import PaymentModuleConfig
from payment_module.builder import PaymentModule, build_payment_module
from payment_module.ports.clock import SystemClock
from payment_module.ports.publisher import OutboxPublisher
from payment_module.secrets import EnvSecretResolver


def build(engine: AsyncEngine, outbox_publisher: OutboxPublisher | None = None) -> PaymentModule:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    return build_payment_module(
        PaymentModuleConfig(),
        SqlAlchemyUnitOfWorkFactory(sessions, TABLES),
        {"sepay": SePayProvider()},
        EnvSecretResolver(),
        SystemClock(),
        settlement_handler=MarkOrderPaid(),  # option A, step 9
        outcome_observer=RecordOutcome(),
        outbox_publisher=outbox_publisher,  # option B, step 9
        template_checklists={"sepay": SePayTemplateChecklist()},
    )
```

## 5. Onboard a merchant

Onboarding is an operator task you run once per merchant, from an admin screen or a script.
The use cases live in [`application/onboarding.py`](../src/payment_module/application/onboarding.py),
[`application/reference_profiles.py`](../src/payment_module/application/reference_profiles.py)
and [`application/readiness.py`](../src/payment_module/application/readiness.py); each takes an
`actor` and your host checks permissions before calling. The order matters:

1. **Merchant** (`register_merchant`): idempotent on your merchant reference.
2. **Receiving account** (`register_receiving_account`): the bank account payers transfer to.
   Pass `bank_bin` to get a VietQR payload in step 8. One bank account has exactly one owner
   per environment in the installation.
3. **Connection** (`register_connection`): one SePay webhook for the merchant in one
   environment. It starts `pending` in `detect_only` mode and gets a random `locator`, which
   goes into the webhook URL (step 6).
4. **Binding** (`bind_connection_account`): which accounts that webhook reports on.
5. **Reference profile** (`create_reference_profile`): the payment-code shape, a named prefix
   (`ORD`) plus a random suffix. It starts as a `draft`. Profiles are installation-wide, not
   per tenant.
6. **Readiness** (`record_connection_readiness`) for the draft profile: the operator confirms
   each item of the SePay setup checklist (step 6) and gives an evidence reference such as a
   ticket id.
7. **Activate the profile** (`activate_reference_profile`): refused while an `active`
   connection has no readiness for it.
8. **Activate the connection** (`set_connection_status` to `active`): needs a bound account
   and readiness for the active profile. Only now does `create_intent` accept the account.

```python title="host.py"
from payment_module.domain.enums import ConnectionStatus, Environment
from payment_module.ports.provider import ReceivingAccountView
from payment_module.ports.resolvers import ProviderConnection


async def onboard(module: PaymentModule) -> tuple[ProviderConnection, ReceivingAccountView]:
    merchant = await module.register_merchant.execute(TENANT, "shop-1", ADMIN)
    account = await module.register_receiving_account.execute(
        TENANT,
        merchant.id,
        Environment.TEST,
        "VCB",
        "0123456789",
        "DEMO SHOP",
        ADMIN,
        bank_bin="970436",  # the bank's NAPAS BIN; needed for the VietQR payload
    )
    connection = await module.register_connection.execute(
        TENANT, merchant.id, "sepay", Environment.TEST, WEBHOOK_SECRET_REF, ADMIN
    )
    await module.bind_connection_account.execute(TENANT, connection.id, account.id, ADMIN)
    draft = await module.create_reference_profile.execute(
        1, {"order": "ORD"}, 8, "ABCDEFGHJKLMNPQRSTUVWXYZ23456789", ADMIN
    )
    # Each item is a manual step in the SePay dashboard; confirm only what was done.
    checklist = SePayTemplateChecklist().checklist(connection, draft.profile)
    done = {item.key: True for item in checklist.items}
    await module.record_connection_readiness.execute(
        TENANT, connection.id, 1, Environment.TEST, done, "setup-ticket-1", ADMIN
    )
    await module.activate_reference_profile.execute(1, ADMIN)
    await module.set_connection_status.execute(
        TENANT, connection.id, ConnectionStatus.ACTIVE, ADMIN, "SePay setup verified"
    )
    return connection, account
```

## 6. Configure the SePay webhook

In the SePay dashboard, for the connection's environment, do what the checklist from
[`SePayTemplateChecklist.checklist`](../src/payment_module/adapters/sepay/checklist.py) lists:

- Turn on payment-code recognition and add one recognition template per prefix (`ORD`, suffix
  length and alphabet as in the profile).
- Point the webhook at `https://<your-host>/webhooks/sepay/<connection.locator>`. The locator
  only selects the connection; it is not a secret.
- Choose HMAC-SHA256 authentication and set the secret to the value of the variable named by
  the connection's secret reference (`SEPAY_WEBHOOK_SECRET` above). SePay signs
  `"{timestamp}." + raw_body`; the module verifies the raw bytes and rejects a timestamp more
  than 300 seconds off (`timestamp_tolerance_seconds` on the connection, 60 to 7200).
- Allow the prefix in the webhook's code filter and limit the webhook to the bound accounts.

The payload fields and SePay behaviour behind these rules are described in
[sepay-integration.md](../sepay-integration.md).

## 7. Mount the webhook router and run the worker

[`make_webhook_router`](../src/payment_module/adapters/fastapi/router.py) answers 200
`{"success": true}` only after the delivery is committed to the inbox, duplicates included;
404 for an unknown or disabled connection, 401 for a bad signature or timestamp, 413 for an
oversized body and 500 for anything else so that SePay retries.

[`PaymentWorker`](../src/payment_module/runtime/worker.py) runs the background loops: inbox
processing, outbox dispatch (with a publisher), reconciliation (with readers) and payload
purge. Leases and retries live in the database, so several workers are safe. Here it runs in
the app's lifespan; you can also run `await PaymentWorker(module).run(stop_event)` in a process
of its own.

```python title="host.py"
import asyncio
import contextlib
import os
from collections.abc import AsyncIterator

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine

from payment_module.adapters.fastapi import make_webhook_router
from payment_module.runtime import PaymentWorker


def create_app(module: PaymentModule) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        worker = asyncio.create_task(PaymentWorker(module).run(stop))
        try:
            yield
        finally:
            stop.set()
            await worker

    app = FastAPI(lifespan=lifespan)
    app.include_router(make_webhook_router(module))
    return app


def app_from_env() -> FastAPI:
    """``uvicorn --factory host:app_from_env`` with DATABASE_URL=postgresql+asyncpg://..."""
    return create_app(build(create_async_engine(os.environ["DATABASE_URL"])))
```

## 8. Create an intent and show the transfer instruction

When the customer checks out, create a payment intent for the order's final amount.
[`CreateIntentCommand`](../src/payment_module/application/create_intent.py) names the order
(`host_ref_type`, `host_ref_id`), an `idempotency_key` (a retry with the same request returns
the same intent) and an aware `expires_at`. Passing a joined
[`SqlAlchemyUnitOfWork`](../src/payment_module/adapters/sqlalchemy/uow.py) creates the intent in
your own transaction, so the order and its intent commit together.

The result's `instruction` is what the payer needs: bank, account, the exact amount and the
`payment_reference` to type in the transfer memo. `qr_payload` is a VietQR string (see
[`build_vietqr`](../src/payment_module/adapters/sepay/vietqr.py)) to render as a QR code; it is
`None` when the account has no `bank_bin`.

```python title="host.py"
from datetime import timedelta

from payment_module.adapters.sqlalchemy.uow import SqlAlchemyUnitOfWork
from payment_module.application.create_intent import CreateIntentCommand
from payment_module.ports.provider import TransferInstruction


async def place_order(
    module: PaymentModule,
    engine: AsyncEngine,
    account: ReceivingAccountView,
    order_id: str,
    amount_vnd: int,
) -> TransferInstruction:
    command = CreateIntentCommand(
        TENANT,
        merchant_id=account.merchant_id,
        receiving_account_id=account.id,
        amount_vnd=amount_vnd,
        host_ref_type="order",
        host_ref_id=order_id,
        idempotency_key=order_id,
        expires_at=module.clock.now() + timedelta(minutes=15),
        prefix_name="order",
    )
    async with async_sessionmaker(engine)() as session, session.begin():
        new_order = sa.insert(orders).values(id=order_id, amount_vnd=amount_vnd, status="new")
        await session.execute(new_order)
        created = await module.create_intent.execute(
            command, SqlAlchemyUnitOfWork.joined(session, TABLES)
        )
        link = sa.update(orders).where(orders.c.id == order_id)
        await session.execute(link.values(intent_id=created.intent.id))
    return created.instruction
```

Only an exact amount settles: a transfer of more or less opens a review case instead.
`module.get_intent_status` gives the committed status to show the payer; `module.cancel_intent`
cancels an unpaid intent.

## 9. Receive the outcome

Pick one of two options.

**Option A: handler in the same transaction.** Use it when the host shares the database. The
[`SettlementHandler`](../src/payment_module/ports/handlers.py) runs inside the settlement
transaction through `uow.session`: the order is paid in the same commit as the settlement, or
neither happens. It must not commit, open another transaction or call the network; raising
rolls everything back and the delivery is retried. The optional `OutcomeObserver` sees every
matching result (settled, in review, and so on) in the same transaction.

```python title="host.py"
from payment_module.ports.handlers import SettlementView, TransactionOutcomeView


class MarkOrderPaid:
    async def on_settled(self, uow, settled: SettlementView) -> None:
        paid = sa.update(orders).where(orders.c.id == settled.host_ref_id).values(status="paid")
        if (await uow.session.execute(paid)).rowcount != 1:
            raise LookupError(f"no order {settled.host_ref_id}")  # retried, never lost


class RecordOutcome:
    async def on_outcome(self, uow, outcome: TransactionOutcomeView) -> None:
        last = sa.update(orders).where(orders.c.intent_id == outcome.intent_id)
        await uow.session.execute(last.values(last_outcome=outcome.match_state.value))
```

**Option B: outbox consumer.** Use it when fulfilment runs elsewhere. Every outcome is also
committed as an outbox event (`PaymentSettled`, `PaymentNeedsReview`, `ReviewResolved`, all
`schema_version = 1`); pass an [`OutboxPublisher`](../src/payment_module/ports/publisher.py) to
`build(engine, outbox_publisher=...)` and the worker delivers each event at least once. The
consumer stores a receipt keyed by `event_id` in the same transaction as its effect, so a
redelivery is a no-op. With option B alone, leave out `settlement_handler`.

```python title="host.py"
from sqlalchemy.dialects.postgresql import insert

from payment_module.ports.publisher import OutboxEventView

payment_receipts = sa.Table(
    "payment_receipts",
    metadata,  # created by migrate() with the other host tables
    sa.Column("event_id", sa.Uuid(), primary_key=True),
    sa.Column("order_id", sa.String(64), nullable=False),
)


class OrderReceipts:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def publish(self, event: OutboxEventView) -> None:
        if event.event_type != "PaymentSettled":
            return
        order_id = str(event.payload["host_ref_id"])
        receipt = insert(payment_receipts).values(event_id=event.event_id, order_id=order_id)
        async with self._engine.begin() as db:
            first = await db.execute(receipt.on_conflict_do_nothing().returning(sa.literal(1)))
            if first.first() is not None:  # a redelivered event_id changes nothing
                paid = sa.update(orders).where(orders.c.id == order_id)
                await db.execute(paid.values(status="paid"))
```

## 10. Operations

- **Review queue.** Money that cannot be settled safely (wrong amount, no or unknown reference,
  late, unbound account, and the other
  [`ReviewReason`](../src/payment_module/domain/enums.py) values) opens a case in
  `pm_review_cases`; your host hears about it through `OutcomeObserver` (`review_case_id`) or
  the `PaymentNeedsReview` event. An operator closes it with
  [`module.resolve_review`](../src/payment_module/application/resolve_review.py), for example
  `attach_to_intent`, `accept_late` or `mark_external`. Nothing is auto-refunded.
- **Reconciliation.** Pass `transaction_readers={"sepay": SePayTransactionReader(...)}`
  ([`adapters/sepay/reader.py`](../src/payment_module/adapters/sepay/reader.py)) and an
  `api_credential_ref` on the connection to read SePay's transaction API. The worker then runs
  [`ReconcileScheduler`](../src/payment_module/application/reconcile.py). Connections stay in
  `detect_only`: a transaction seen only through the API opens a review case instead of
  settling.
- **The `auto_settle` gate.** `module.set_reconcile_mode` switches a connection to
  `auto_settle` only when an
  [`EvidenceVerifier`](../src/payment_module/ports/evidence.py) accepts an evidence file for its
  environment and accounts (see `FileEvidenceVerifier` in
  [`adapters/evidence_file.py`](../src/payment_module/adapters/evidence_file.py)). Without a
  verifier every request is rejected. The current state of that evidence is under Known
  limitations in [CHANGELOG.md](../CHANGELOG.md).
- **Retention.** Raw webhook bodies and payer text are kept until
  `PaymentModuleConfig.pii_retention_days` (default `None`: keep until you purge); the worker's
  purge loop runs [`PurgeExpiredPayloads`](../src/payment_module/application/purge.py).
  `validate()` rejects a retention shorter than the reconciliation link horizon.
- **Stuck work.** A delivery or event that failed all its attempts is kept, not dropped:
  `module.requeue_inbox` and `module.requeue_outbox` send it back to the worker once the cause
  is fixed. Metrics go to a
  [`MetricsSink`](../src/payment_module/ports/metrics.py) (`LoggingMetricsSink` in
  `payment_module.runtime` logs them).
- **Profile rotation and legacy codes.** Create the next profile version, record readiness for
  it on every active connection, then activate it; the previous one keeps accepting late
  payments. [`examples/saas_host/app.py`](../examples/saas_host/app.py) shows rotation and the
  import of codes from an older system.

Threat model, secret handling and operational ownership are in
[security-and-operations.md](../security-and-operations.md); the design documents are indexed
in [readme.vi.md](../readme.vi.md).
