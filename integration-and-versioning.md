# Tích hợp host, đóng gói và nâng cấp

[← Mục lục](readme.md) · [Hai phương án host](diagrams/12-host-integration.md)

## Package

Một distribution Python `payment-module` (import `payment_module`), module nội bộ tách core/ports/provider/storage/framework integration; optional extras `sqlalchemy`, `postgres`, `fastapi`, `sepay` để host chọn. Bản đầu `0.1.0` phát hành local-only: tag git `v0.1.0` + wheel build tái lập, không PyPI/private index. Cài từ wheel:

```sh
uv pip install "payment_module-0.1.0-py3-none-any.whl[sqlalchemy,postgres,fastapi,sepay]"
```

Khi repo có remote, host có thể pin `payment-module @ git+ssh://<remote>@v0.1.0`. Digest wheel/sdist và `SOURCE_DATE_EPOCH` nằm trong annotated tag (`git show v0.1.0`); [CHANGELOG](CHANGELOG.md) ghi thay đổi theo bốn contract.

Tách nhiều distribution khi dependency/release cycle cần độc lập. Contract provider dựa capabilities, không ép refund/recurring/payout vào base interface. Pin version, changelog và migration notes; test compatibility host trước upgrade.

## Host onboarding

Host tính tổng phải trả (gồm thuế/phí) trước `CreateIntent`; package không biết VAT. Host cấu hình provider registry, UoW, secret/config resolver, reference profile, matching policy và business handler. Đăng ký router webhook và worker entry points tại composition root. Merchant account onboarding và readiness theo [SePay](sepay-integration.md). UI/realtime lấy trạng thái đã commit; không tự tính kết quả thanh toán từ query browser.

Hai host mẫu cài cùng một wheel và là điểm bắt đầu khi tích hợp: [`examples/saas_host`](examples/saas_host) (phương án A — FastAPI, handler cùng UnitOfWork, `OutcomeObserver`) và [`examples/fnb_host`](examples/fnb_host) (phương án B — không FastAPI, outbox → consumer idempotent theo `event_id`).

## Handler cùng UnitOfWork

Dùng khi host cùng database và cần atomic payment + entitlement. Handler nhận transaction context, không commit riêng, không gọi API bên ngoài; exception rollback whole settlement transaction. Tránh callback tùy ý làm transaction kéo dài.

## Handler bất đồng bộ

Payment commit outbox, host consume bằng event_id, lưu receipt và fulfillment state. Có retry, backlog và operator recovery. Bên nhận cần schema versioning để nâng cấp rolling không làm mất sự kiện. Idempotency ngoài DB của host phụ thuộc contract downstream.

## Quan hệ với MeowAI (tham chiếu chỉ đọc)

MeowAI là tham chiếu hành vi chỉ đọc (quyết định N1); package được viết mới từ thiết kế, không copy/port code. Tích hợp MeowAI là plan riêng (N2). Sáu bước dưới đây áp dụng cho plan tích hợp tương lai, không thuộc phạm vi xây package:

1. Xác minh lại source, tests và contract thực tế trước triển khai; các ghi nhận trước chỉ là snapshot nghiên cứu.
2. Tách provider normalization/auth khỏi entitlement/order policy, giữ nguyên các bất biến và API hiện có nếu chưa duyệt thay đổi.
3. Xây adapter host và contract tests, giữ payment + entitlement cùng UoW trong lần tích hợp đầu nếu vẫn cùng DB.
4. Di chuyển schema có migration/backfill đối chiếu; không chạy hai writer fulfillment cùng lúc trong cutover.
5. So sánh kết quả lịch sử và retry paths trong môi trường thử nghiệm; chuẩn bị recovery trước chuyển production.
6. Kiểm chứng bằng project thứ hai có nghiệp vụ khác, không fork core.

## Versioning

Chính sách version 0.x (áp dụng từ `0.1.0`):

- Version theo SemVer với tiền tố `0.`: trong 0.x, thay đổi phá vỡ ở một trong bốn contract dưới đây bump **minor** (`0.1 → 0.2`) và phải ghi rõ trong CHANGELOG kèm migration notes; sửa lỗi và thay đổi cộng thêm bump **patch**.
- Bốn contract được theo dõi: public Python API (use case, port, DTO), event `(type, schema_version)`, provider normalization, database schema (`schema_vN` đóng băng, nâng cấp bằng `schema_vN.upgrade_from_v(N-1)`).
- Event: thêm trường không bump `schema_version`; bỏ hoặc đổi nghĩa trường thì bump; consumer bỏ qua trường lạ và lưu receipt theo `event_id`.
- Database schema mới là `schema_vN` + `schema_vN.upgrade_from_v(N-1)` + migration note; không sửa `schema_v1` sau `0.1.0`.
- Public API của `0.1.0`: `build_payment_module`, các use case, port, DTO, event `(type, schema_version=1)`, `define_tables`/`schema_v1`.
- Mỗi release có CHANGELOG với bốn mục Public API / Event schema / Normalization / DB schema.
- Phát hành bằng annotated tag git + wheel build tái lập (`make build` đặt `SOURCE_DATE_EPOCH` = thời điểm commit của `HEAD`; digest ghi trong tag message); consumer pin theo tag hoặc wheel.

Public Python API, event schema, provider normalization và database schema đều là contract cần compatibility policy. Breaking change cần release/migration có chủ đích. Host quyết định lúc migrate sau backup, không auto-stamp; rollback code không bảo đảm rollback schema được. Kiểm tra provider Test/Live và profile legacy sau upgrade.
