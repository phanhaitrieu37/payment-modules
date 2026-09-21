# Kiến trúc và ranh giới Payment

[← Mục lục](readme.md)

## Outcome

Project mới có thể tích hợp thanh toán SePay bằng một package chung, cấu hình merchant và phần mở rộng nghiệp vụ, không copy hoặc fork lõi. Nhiều merchant trong một project nhận tiền trực tiếp vào tài khoản của mình. Mẫu mã có prefix riêng theo project.

## Phân lớp

| Lớp | Chủ sở hữu |
|---|---|
| Domain | Money, intent, transaction, settlement, invariant |
| Application | Tạo intent, tiếp nhận webhook, xử lý inbox, đối soát và review |
| Ports | Provider, store/UoW, policy, reference generator và handler |
| Adapters | SePay, SQLAlchemy/PostgreSQL, router FastAPI |
| Host | Auth người dùng, permission mapping, giá/đơn/thuế, fulfillment, realtime UI |

Dependency hướng vào lõi. Domain không import ORM, FastAPI hay package MeowAI/restaurant. Host là composition root lắp ghép các implementation. Module giữ thông tin tham chiếu nghiệp vụ có kiểu rõ, không chứa toàn bộ model đơn hàng trong metadata.

## Cách triển khai

Đề xuất package Python chạy trong backend từng project. Database riêng theo project; tenant/merchant được cách ly bên trong dự án. Worker có thể chạy process riêng dùng cùng database và package. Cloud payment service dùng chung mọi project không thuộc mô hình đã chọn.

FastAPI là tích hợp tuỳ chọn, SQLAlchemy/PostgreSQL là adapter chuẩn đề xuất. Realtime thông báo thuộc host, không bắt payment core phụ thuộc SSE/WebSocket hoặc Redis. Outbox bảo đảm đường phát sự kiện có thể phục hồi; Redis Pub/Sub nếu dùng chỉ là kênh chuyển tiếp, không phải nơi giữ bằng chứng thanh toán.

## Lựa chọn và đánh đổi

| Cách tiếp cận | Giả định chính | Điểm thất bại đầu tiên |
|---|---|---|
| Copy code mỗi project | Ít thay đổi, có người đồng bộ sửa lỗi | Sửa lỗi provider/dedup không được áp dụng đồng đều |
| Package + adapter — chọn | Các project chủ yếu Python, chấp nhận quản lý version | Nhiều host khác ngôn ngữ hoặc bỏ nâng cấp dependency |
| Service tập trung | Muốn vận hành tập trung và chấp nhận mạng/service boundary | Khó giữ transaction nguyên tử với nghiệp vụ từng host |

Package không miễn phí bảo trì: cần contract tests, migration và compatibility policy. Nếu nhiều host chuyển sang TypeScript, cân nhắc port theo cùng contract hoặc service API. Chưa có benchmark chứng minh chuyển Node.js cải thiện đủ lớn cho workload này; language không giải quyết correctness của tiền, retry hoặc độ trễ provider.

## Sơ đồ

- [Tổng quan](diagrams/01-architecture-overview.md)
- [Component dependencies](diagrams/02-component-dependencies.md)
