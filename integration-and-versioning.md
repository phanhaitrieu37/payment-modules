# Tích hợp host, đóng gói và nâng cấp

[← Mục lục](readme.md) · [Hai phương án host](diagrams/12-host-integration.md)

## Package

Đề xuất một distribution Python ban đầu, module nội bộ tách core/ports/provider/storage/framework integration; optional extras để host chọn FastAPI/SQLAlchemy. Không có package đã phát hành, nên chưa đưa lệnh cài đặt hoặc version hỗ trợ như thông tin thật.

Tách nhiều distribution khi dependency/release cycle cần độc lập. Contract provider dựa capabilities, không ép refund/recurring/payout vào base interface. Pin version, changelog và migration notes; test compatibility host trước upgrade.

## Host onboarding

Host cấu hình provider registry, UoW, secret/config resolver, reference profile, matching policy và business handler. Đăng ký router webhook và worker entry points tại composition root. Merchant account onboarding và readiness theo [SePay](sepay-integration.md). UI/realtime lấy trạng thái đã commit; không tự tính kết quả thanh toán từ query browser.

## Handler cùng UnitOfWork

Dùng khi host cùng database và cần atomic payment + entitlement. Handler nhận transaction context, không commit riêng, không gọi API bên ngoài; exception rollback whole settlement transaction. Tránh callback tùy ý làm transaction kéo dài.

## Handler bất đồng bộ

Payment commit outbox, host consume bằng event_id, lưu receipt và fulfillment state. Có retry, backlog và operator recovery. Bên nhận cần schema versioning để nâng cấp rolling không làm mất sự kiện. Idempotency ngoài DB của host phụ thuộc contract downstream.

## Tách từ MeowAI

1. Xác minh lại source, tests và contract thực tế trước triển khai; các ghi nhận trước chỉ là snapshot nghiên cứu.
2. Tách provider normalization/auth khỏi entitlement/order policy, giữ nguyên các bất biến và API hiện có nếu chưa duyệt thay đổi.
3. Xây adapter host và contract tests, giữ payment + entitlement cùng UoW trong lần tích hợp đầu nếu vẫn cùng DB.
4. Di chuyển schema có migration/backfill đối chiếu; không chạy hai writer fulfillment cùng lúc trong cutover.
5. So sánh kết quả lịch sử và retry paths trong môi trường thử nghiệm; chuẩn bị recovery trước chuyển production.
6. Kiểm chứng bằng project thứ hai có nghiệp vụ khác, không fork core.

## Versioning

Public Python API, event schema, provider normalization và database schema đều là contract cần compatibility policy. Breaking change cần release/migration có chủ đích. Host quyết định lúc migrate sau backup, không auto-stamp; rollback code không bảo đảm rollback schema được. Kiểm tra provider Test/Live và profile legacy sau upgrade.
