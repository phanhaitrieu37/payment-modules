# Pipeline, trạng thái và phục hồi

[← Mục lục](readme.md)

## Happy path

Host tính giá → create intent idempotent → module lưu snapshot/reference → trả QR → khách chuyển khoản → provider webhook → verify → durable inbox commit → ACK → worker chuẩn hoá fact → guard receiver/tenant/incoming → dedup và khóa intent → policy → settlement + paid + outbox commit → host nhận kết quả.

Xem [sequence thành công](diagrams/06-sequence-success.md) và [pipeline](diagrams/11-processing-pipeline.md). Đọc trạng thái đã commit là cách UI xác nhận; redirect, screenshot hay QR không là bằng chứng tiền vào.

## Ranh giới transaction

Inbox là transaction tiếp nhận riêng. Worker dùng lease để claim công việc, hoàn thành xử lý qua transaction có unique constraints và lock. Lease hết hạn cho phép worker khác retry; correctness dựa database, không chỉ timer/lock RAM. Fact đã được tiếp nhận trong inbox không bị mất nếu transaction settlement thất bại.

Settlement, intent paid và outbox nguyên tử. Business handler cùng DB/UoW có thể tham gia cùng transaction; lỗi handler rollback settlement và retry từ inbox. Handler bất đồng bộ chạy sau payment commit có trạng thái riêng, không rollback tiền đã nhận. Không gọi dịch vụ ngoài trong transaction giữ khóa lâu.

## Các đường lỗi

| Sự kiện | Kết quả cần bảo đảm |
|---|---|
| Sai auth/replay timestamp | Từ chối, không tạo payment |
| DB lỗi trước inbox commit | Không ACK thành công |
| Webhook gửi trùng | Ghi nhận receipt an toàn, không thêm tiền |
| Worker chết sau claim | Lease hết hạn và retry |
| Chết sau commit trước publish/ACK | Replay idempotent, sự kiện không mất |
| Receiver sai hoặc tiền ra | Không settle; lưu thông tin phù hợp để audit/review |
| Không có mã hoặc mã mơ hồ | Lưu unmatched/review, không đoán |
| Lệch tiền/late payment | Không auto-settle theo policy mặc định đề xuất |
| Host fulfillment lỗi async | Payment vẫn paid, retry fulfillment |
| Webhook bị provider lọc | Đối soát API, parser trong endpoint không thể cứu |

## Reconciliation và review

Chạy lại cửa sổ có overlap an toàn. API và webhook phải đi qua cùng normalization/invariant/dedup/matching semantics, nhưng không giả API receipt có chữ ký webhook. Nguồn API phải được xác thực theo credential của đúng connection. Identity chưa chắc → review thay vì tự settle.

Review phải có quyền, actor, reason và audit. Cho phép rematch khi intent tới muộn nhưng không tạo hai settlement. Operator không được bypass receiver/tenant invariant. Expired intent nhận tiền muộn có thể được xử lý theo quyết định nghiệp vụ được duyệt; sơ đồ operator transition là đề xuất, không phải policy đã triển khai.

## Event delivery

Outbox event có event_id, aggregate identifier, type, schema version và trusted scope. Consumer lưu receipt và local side effect cùng transaction nếu có thể. Delivery at-least-once; consumer phải chống trùng. Nếu gọi dịch vụ ngoài cần idempotency contract của dịch vụ đó hoặc phương án recovery riêng, không hứa exactly-once tổng thể.

Các state machine: [PaymentIntent](diagrams/09-state-payment-intent.md), [Inbox/transaction/fulfillment](diagrams/10-state-inbox-transaction.md), [failure sequence](diagrams/07-sequence-failure-retry.md), [reconciliation](diagrams/08-sequence-reconcile.md).
