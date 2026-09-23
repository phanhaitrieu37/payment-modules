# Bắt đầu tích hợp

[English](getting-started.md) · **Tiếng Việt**

Hướng dẫn này đưa một project host mới từ con số không đến một đơn hàng đã thanh toán: cài
`payment-module`, tạo bảng, nối module, onboard một merchant, nhận một webhook SePay có chữ ký
và đánh dấu đơn đã trả. Tài liệu viết cho cả lập trình viên lẫn coding agent.

Các code block có tiêu đề `host.py`, đọc từ trên xuống, ghép thành một module host chạy được.
[`tests/integration/docs/test_getting_started.py`](../tests/integration/docs/test_getting_started.py)
trích và chạy chúng trên PostgreSQL, nên chúng luôn khớp với code đã phát hành. Với host lớn
hơn, xem [`examples/saas_host`](../examples/saas_host) (FastAPI, settle trong transaction của
host, import mã cũ) và [`examples/fnb_host`](../examples/fnb_host) (không web framework,
consumer outbox).

Phân chia trách nhiệm: module xác nhận, lưu và đối chiếu tiền về tài khoản ngân hàng của chính
merchant. Host giữ giá, đơn hàng, thuế và quyền lợi, và gọi module với số tiền cuối cùng cần trả.

## 0. Yêu cầu

- Python 3.12 trở lên.
- PostgreSQL. Schema dùng tính năng riêng của PostgreSQL (partial unique index, `JSONB`,
  `SKIP LOCKED`); driver async là `asyncpg`.
- Tài khoản SePay đã liên kết tài khoản ngân hàng nhận tiền, có quyền vào phần cấu hình webhook
  và mã thanh toán. Test và Live là hai môi trường riêng; bắt đầu với Test.

## 1. Cài đặt

Cài wheel từ GitHub Release, kèm các extras host cần:

```sh
uv pip install "payment-module[sqlalchemy,postgres,fastapi,sepay] @ https://github.com/phanhaitrieu37/payment-modules/releases/download/v0.1.0/payment_module-0.1.0-py3-none-any.whl"
```

Hoặc pin theo tag git:

```sh
uv pip install "payment-module[sqlalchemy,postgres,fastapi,sepay] @ git+https://github.com/phanhaitrieu37/payment-modules@v0.1.0"
```

Trang release có file `SHA256SUMS`; so với kết quả `shasum -a 256` của wheel đã tải. Extras:
`sqlalchemy` (bảng, repository, unit of work, Alembic), `postgres` (asyncpg), `fastapi` (webhook
router, bỏ nếu host không có ứng dụng FastAPI), `sepay` (provider, API reader, VietQR). Bốn
contract có version (public API, event schema, normalization, DB schema) và chính sách 0.x nằm
trong [CHANGELOG.md](../CHANGELOG.md).

## 2. Tạo schema

Bảng của module được định nghĩa bởi
[`define_tables`](../src/payment_module/adapters/sqlalchemy/tables.py) (mọi tên có tiền tố
`pm_`) và được tạo bởi migration đóng băng
[`schema_v1.upgrade`](../src/payment_module/adapters/sqlalchemy/migrations/schema_v1.py).
Không có gì chạy khi import: host tự chạy migration, sau khi backup. Trong một revision Alembic,
gọi `schema_v1.upgrade(op)` với `op` của Alembic; hàm dưới đây làm điều tương tự ngoài Alembic,
rồi tạo bảng riêng của host.

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

Schema version sau sẽ là một module mới có bước upgrade; `schema_v1` không bao giờ thay đổi.

## 3. Secret và cấu hình

- **Webhook secret.** Connection chỉ lưu một tham chiếu như `env:SEPAY_WEBHOOK_SECRET`.
  [`EnvSecretResolver`](../src/payment_module/secrets/env_secret_resolver.py) đọc biến môi
  trường đó, và cả `SEPAY_WEBHOOK_SECRET_PREVIOUS` trong lúc xoay secret. Implement port
  [`SecretResolver`](../src/payment_module/ports/resolvers.py) để đọc từ secret store của host.
- **Cấu hình module.** [`PaymentModuleConfig`](../src/payment_module/application/config.py) có
  giá trị mặc định an toàn cho mọi trường; `build_payment_module` gọi `validate()` và từ chối
  cấu hình có thể làm mất bằng chứng tiền. Trường host hay đặt nhất là `pii_retention_days`
  (xem [Vận hành](#10-vận-hành)).
- **Environment và tenant.** `tenant_id` là khoá cô lập của host; nó đến từ context đã xác thực
  của host, không bao giờ từ người trả. Mỗi tài khoản và connection thuộc đúng một environment,
  `test` hoặc `live`.

```python title="host.py"
TENANT = "demo-shop"  # from the host's authenticated context in a real app
ADMIN = "admin@demo-shop.example"  # the operator recorded on onboarding steps
WEBHOOK_SECRET_REF = "env:SEPAY_WEBHOOK_SECRET"
```

## 4. Dựng module

[`build_payment_module`](../src/payment_module/builder.py) nối mọi use case một lần và trả về
`PaymentModule`; giữ một bản cho mỗi process. Hàm cần unit-of-work factory trên engine của host,
provider registry, secret resolver và clock. Các tham số tuỳ chọn quyết định cách host nhận kết
quả thanh toán (bước 9).

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

## 5. Onboard merchant

Onboarding là việc của operator, chạy một lần cho mỗi merchant, từ màn hình admin hoặc script.
Các use case nằm trong [`application/onboarding.py`](../src/payment_module/application/onboarding.py),
[`application/reference_profiles.py`](../src/payment_module/application/reference_profiles.py)
và [`application/readiness.py`](../src/payment_module/application/readiness.py); mỗi bước nhận
một `actor`, và host kiểm tra quyền trước khi gọi. Thứ tự là bắt buộc:

1. **Merchant** (`register_merchant`): idempotent theo mã merchant của host.
2. **Tài khoản nhận tiền** (`register_receiving_account`): tài khoản ngân hàng người trả chuyển
   vào. Truyền `bank_bin` để có payload VietQR ở bước 8. Một tài khoản ngân hàng chỉ có đúng một
   chủ trong mỗi environment của bản cài.
3. **Connection** (`register_connection`): một webhook SePay của merchant trong một
   environment. Connection bắt đầu ở `pending`, chế độ `detect_only`, và nhận một `locator` ngẫu
   nhiên dùng trong URL webhook (bước 6).
4. **Binding** (`bind_connection_account`): webhook đó báo về những tài khoản nào.
5. **Reference profile** (`create_reference_profile`): dạng mã thanh toán, gồm prefix có tên
   (`ORD`) và suffix ngẫu nhiên. Profile bắt đầu ở `draft`. Profile là cấp bản cài, không theo
   tenant.
6. **Readiness** (`record_connection_readiness`) cho profile draft: operator xác nhận từng mục
   trong checklist cấu hình SePay (bước 6) và ghi một tham chiếu bằng chứng, ví dụ mã ticket.
7. **Kích hoạt profile** (`activate_reference_profile`): bị từ chối khi còn connection `active`
   chưa có readiness cho profile đó.
8. **Kích hoạt connection** (`set_connection_status` sang `active`): cần tài khoản đã bind và
   readiness cho profile đang active. Chỉ từ lúc này `create_intent` mới nhận tài khoản.

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

## 6. Cấu hình webhook SePay

Trong dashboard SePay, ở đúng environment của connection, làm theo checklist của
[`SePayTemplateChecklist.checklist`](../src/payment_module/adapters/sepay/checklist.py):

- Bật nhận diện mã thanh toán và thêm một mẫu nhận diện cho mỗi prefix (`ORD`, độ dài suffix và
  bảng ký tự như trong profile).
- Trỏ webhook tới `https://<your-host>/webhooks/sepay/<connection.locator>`. Locator chỉ dùng để
  chọn connection, không phải secret.
- Chọn xác thực HMAC-SHA256 và đặt secret bằng giá trị của biến môi trường mà secret reference
  của connection trỏ tới (`SEPAY_WEBHOOK_SECRET` ở trên). SePay ký `"{timestamp}." + raw_body`;
  module verify đúng bytes gốc và từ chối timestamp lệch quá 300 giây
  (`timestamp_tolerance_seconds` của connection, từ 60 đến 7200).
- Cho phép prefix trong bộ lọc mã của webhook và giới hạn webhook vào các tài khoản đã bind.

Các trường payload và hành vi SePay đằng sau những quy tắc này được mô tả ở
[sepay-integration.md](../sepay-integration.md).

## 7. Gắn webhook router và chạy worker

[`make_webhook_router`](../src/payment_module/adapters/fastapi/router.py) chỉ trả 200
`{"success": true}` sau khi delivery đã commit vào inbox, kể cả delivery trùng; 404 cho
connection không tồn tại hoặc đã tắt, 401 cho chữ ký hoặc timestamp sai, 413 cho body quá lớn và
500 cho mọi lỗi khác để SePay gửi lại.

[`PaymentWorker`](../src/payment_module/runtime/worker.py) chạy các vòng lặp nền: xử lý inbox,
gửi outbox (khi có publisher), đối soát (khi có reader) và purge payload. Lease và retry nằm
trong database nên chạy nhiều worker vẫn an toàn. Ở đây worker chạy trong lifespan của app; cũng
có thể chạy `await PaymentWorker(module).run(stop_event)` trong một process riêng.

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

## 8. Tạo intent và hiển thị hướng dẫn chuyển khoản

Khi khách checkout, tạo một payment intent với số tiền cuối cùng của đơn.
[`CreateIntentCommand`](../src/payment_module/application/create_intent.py) ghi đơn hàng
(`host_ref_type`, `host_ref_id`), một `idempotency_key` (gọi lại cùng request trả về cùng
intent) và `expires_at` có timezone. Truyền một
[`SqlAlchemyUnitOfWork`](../src/payment_module/adapters/sqlalchemy/uow.py) dạng joined để tạo
intent trong transaction của host, nên đơn hàng và intent cùng commit.

`instruction` trong kết quả là thứ người trả cần: ngân hàng, số tài khoản, đúng số tiền và
`payment_reference` để ghi vào nội dung chuyển khoản. `qr_payload` là chuỗi VietQR (xem
[`build_vietqr`](../src/payment_module/adapters/sepay/vietqr.py)) để vẽ thành mã QR; giá trị là
`None` khi tài khoản không có `bank_bin`.

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

Chỉ đúng số tiền mới settle: chuyển thừa hoặc thiếu sẽ mở review case. `module.get_intent_status`
trả trạng thái đã commit để hiển thị cho người trả; `module.cancel_intent` huỷ intent chưa trả.

## 9. Nhận kết quả

Chọn một trong hai phương án.

**Phương án A: handler trong cùng transaction.** Dùng khi host chung database.
[`SettlementHandler`](../src/payment_module/ports/handlers.py) chạy bên trong transaction
settle qua `uow.session`: đơn được đánh dấu đã trả trong cùng commit với settlement, hoặc không
có gì xảy ra. Handler không được commit, mở transaction khác hay gọi mạng; raise thì toàn bộ
rollback và delivery được thử lại. `OutcomeObserver` (tuỳ chọn) thấy mọi kết quả matching
(settled, in review, ...) trong cùng transaction.

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

**Phương án B: consumer outbox.** Dùng khi fulfillment chạy ở nơi khác. Mọi kết quả cũng được
commit thành event outbox (`PaymentSettled`, `PaymentNeedsReview`, `ReviewResolved`, đều
`schema_version = 1`); truyền một [`OutboxPublisher`](../src/payment_module/ports/publisher.py)
vào `build(engine, outbox_publisher=...)` và worker giao mỗi event ít nhất một lần. Consumer lưu
receipt theo `event_id` trong cùng transaction với tác động của nó, nên giao lại là no-op. Nếu
chỉ dùng phương án B, bỏ `settlement_handler`.

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

## 10. Vận hành

- **Hàng đợi review.** Tiền không settle an toàn được (sai số tiền, thiếu hoặc lạ mã, trễ, tài
  khoản chưa bind, và các giá trị khác của
  [`ReviewReason`](../src/payment_module/domain/enums.py)) mở một case trong `pm_review_cases`;
  host biết qua `OutcomeObserver` (`review_case_id`) hoặc event `PaymentNeedsReview`. Operator
  đóng case bằng [`module.resolve_review`](../src/payment_module/application/resolve_review.py),
  ví dụ `attach_to_intent`, `accept_late` hoặc `mark_external`. Không có hoàn tiền tự động.
- **Đối soát.** Truyền `transaction_readers={"sepay": SePayTransactionReader(...)}`
  ([`adapters/sepay/reader.py`](../src/payment_module/adapters/sepay/reader.py)) và
  `api_credential_ref` trên connection để đọc API giao dịch của SePay. Khi đó worker chạy
  [`ReconcileScheduler`](../src/payment_module/application/reconcile.py). Connection vẫn ở
  `detect_only`: giao dịch chỉ thấy qua API sẽ mở review case thay vì settle.
- **Cổng `auto_settle`.** `module.set_reconcile_mode` chỉ chuyển connection sang `auto_settle`
  khi một [`EvidenceVerifier`](../src/payment_module/ports/evidence.py) chấp nhận file bằng
  chứng cho environment và các tài khoản của nó (xem `FileEvidenceVerifier` trong
  [`adapters/evidence_file.py`](../src/payment_module/adapters/evidence_file.py)). Không có
  verifier thì mọi yêu cầu đều bị từ chối. Tình trạng hiện tại của bằng chứng nằm ở mục Known
  limitations trong [CHANGELOG.md](../CHANGELOG.md).
- **Lưu giữ dữ liệu.** Raw body webhook và nội dung của người trả được giữ đến
  `PaymentModuleConfig.pii_retention_days` (mặc định `None`: giữ đến khi host purge); vòng purge
  của worker chạy [`PurgeExpiredPayloads`](../src/payment_module/application/purge.py).
  `validate()` từ chối thời hạn ngắn hơn link horizon của đối soát.
- **Việc bị kẹt.** Delivery hoặc event đã thất bại hết số lần thử được giữ lại, không bị bỏ:
  `module.requeue_inbox` và `module.requeue_outbox` đưa nó về worker sau khi đã sửa nguyên
  nhân. Metrics đi vào một [`MetricsSink`](../src/payment_module/ports/metrics.py)
  (`LoggingMetricsSink` trong `payment_module.runtime` ghi chúng ra log).
- **Xoay profile và mã cũ.** Tạo profile version tiếp theo, ghi readiness cho nó trên mọi
  connection đang active, rồi kích hoạt; profile trước vẫn nhận thanh toán trễ.
  [`examples/saas_host/app.py`](../examples/saas_host/app.py) minh hoạ việc xoay profile và
  import mã từ hệ thống cũ.

Threat model, xử lý secret và phân chia trách nhiệm vận hành nằm ở
[security-and-operations.md](../security-and-operations.md); mục lục tài liệu thiết kế ở
[readme.vi.md](../readme.vi.md).
