# Quyết định, đánh đổi và câu hỏi mở

[← Mục lục](readme.md)

## Người dùng đã quyết định

- Tư vấn kiến trúc Payment dùng lại; restaurant chỉ là case tích hợp.
- Mỗi project cài module và tự vận hành, chủ yếu Python.
- Một project hỗ trợ nhiều đơn vị nhận tiền ngay từ đầu.
- Khách chuyển khoản trực tiếp vào tài khoản merchant; không thu hộ/payout.
- Prefix cấu hình tùy project; đưa cơ chế mẫu mã SePay vào kiến trúc.
- Tài liệu/sơ đồ được public qua Orca và nay xuất đầy đủ sang thư mục riêng.

## Phương án thiết kế đang dùng

Ports & Adapters, interface nhỏ/composition, PostgreSQL/SQLAlchemy adapter, FastAPI tùy chọn, HMAC, durable inbox, settlement/outbox atomic, exact-amount default + review, handler cùng UoW hoặc async. Đây là baseline để triển khai tiếp; tên API/bảng và default cụ thể chưa phải code contract đã phát hành.

Versioned ReferenceProfile thay prefix-only: ghi rõ suffix/alphabet/version, snapshot và readiness merchant. Giả định dashboard SePay được cấu hình đúng; rủi ro đầu tiên là drift hoặc template overlap. Chi phí là thêm config history/checklist và kiểm tra cutover. Hai bảng là đề xuất chứ không phải yêu cầu bắt buộc.

Không có phương án tốt hơn được chứng minh cho mục tiêu đã chốt. Prefix-only đơn giản hơn nhưng chuyển complexity thành quy ước ẩn. Autoprovision có thể tốt hơn nếu có API chính thức đủ quyền/khả năng kiểm tra; hiện chưa có chứng cứ. Đổi provider hoặc framework thông qua adapter; chuyển sang nhiều ngôn ngữ có chi phí API service/port và versioning.

## Không đưa vào scope

Marketplace plugin động, microservice bắt buộc, thu hộ/split/payout, restaurant infrastructure, e-invoice/VAT và subscription trong core, partial/refund như capability đã cam kết. Không đặt tên recurring/refund method giả chỉ để interface trông tổng quát.

## Câu hỏi và bằng chứng còn thiếu

1. Mapping numeric webhook ID và API v2 UUID/reference nào xác định cùng giao dịch?
2. Canonical receiving-account/VA identity, trường đầy đủ để đối chiếu và quan hệ một connection với nhiều account là gì? Ví dụ payload có accountNumber/subAccount; cách bind thực tế chưa kiểm chứng.
3. Regex boundary của SePay: overlong suffix, embedded code, adjacent characters, multiple codes xử lý thế nào?
4. Suffix default, retry budget collision, scope uniqueness cross-environment và legacy retirement window?
5. API quản trị template có tồn tại/phù hợp không? Hiện không hứa auto-sync.
6. Late/under/overpayment review, operator authority, partial payment/refund nếu cần sau này?
7. Retention raw payload, auth/permission contracts, throughput, rate-limit fairness và RPO/RTO?
8. Chốt schema về provenance nhiều delivery cùng fact và scope project config; không dùng masked data để match.
9. Host nào chọn UoW và host nào async; schema event/compatibility policy cụ thể?

Các câu hỏi này không chặn việc đọc bản thiết kế, nhưng cần giải quyết trước các phần implementation/production phụ thuộc chúng.
