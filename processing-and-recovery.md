# Pipeline, trạng thái và phục hồi

[← Mục lục](readme.md)

Trạng thái: đã đối chiếu với các delta đã duyệt (22/09/2026); thiết kế, chưa triển khai.

## Happy path

Host tính tổng phải trả → create intent idempotent (chọn tên prefix) → module lưu snapshot/reference → trả QR → khách chuyển khoản → provider webhook → verify HMAC → đọc `id` → durable inbox commit → ACK → worker normalize → ghi observation + fact canonical (dedup) → guard (chiều tiền, rồi receiver/merchant) → resolver (khớp nguyên reference) → eligibility → policy → post-check đúng số tiền + khoá intent → settlement + paid + outbox commit → host nhận kết quả.

Thứ tự rút gọn: **fact → guard → resolver → eligibility → policy → post-check**. Fact luôn được ghi trước mọi quyết định; dedup nằm ở bước ghi fact (unique `dedup_key`), không phải sau guard.

Xem [sequence thành công](diagrams/06-sequence-success.md) và [pipeline](diagrams/11-processing-pipeline.md). Đọc trạng thái đã commit là cách UI xác nhận; redirect, screenshot hay QR không là bằng chứng tiền vào.

## Ranh giới transaction

Inbox là transaction tiếp nhận riêng. Claim, xử lý, ghi lỗi/retry và thu hồi lease hết hạn là **các transaction tách biệt có fencing**: mỗi lần claim tăng `lease_generation`; finalize thành công hoặc lỗi là compare-and-set theo generation, nên worker cũ không ghi đè sau khi lease đã bị claim lại. Rollback transaction xử lý (kể cả lỗi handler cùng UoW) không làm mất việc: transaction ghi lỗi riêng đặt `retry_wait`, hoặc lease hết hạn cho worker khác claim. Correctness dựa database, không chỉ timer/lock RAM. Fact đã được tiếp nhận trong inbox không bị mất nếu transaction settlement thất bại.

Webhook và API đến đồng thời: link-or-create fact canonical chạy dưới khoá theo scope `tenant + environment + provider + provider_account_key` (hoặc SERIALIZABLE có retry giới hạn) và đọc lại trước khi tạo, nên chỉ tạo **một** canonical khi có đủ bằng chứng link; khi chưa đủ, giữ provenance và review thay vì gộp.

Kích hoạt profile mới đổi version active **trong một transaction** (mới `active`, cũ `accepted_legacy`). Xem [ma trận trạng thái và ranh giới transaction](data-model.md#ma-trận-trạng-thái-và-ranh-giới-transaction).

Settlement, intent paid và outbox nguyên tử. Business handler cùng DB/UoW có thể tham gia cùng transaction; lỗi handler rollback settlement và retry từ inbox. Handler bất đồng bộ chạy sau payment commit có trạng thái riêng, không rollback tiền đã nhận. Không gọi dịch vụ ngoài trong transaction giữ khóa lâu.

## Các đường lỗi

| Sự kiện | Kết quả cần bảo đảm |
|---|---|
| Locator lạ hoặc connection `disabled` | 404 đồng nhất, không lộ connection nào tồn tại |
| Sai auth/replay timestamp | 401, không tạo payment (cửa sổ mặc định 300 giây tới khi SePay Test xác minh timestamp khi retry) |
| Đã verify nhưng thiếu `id` | Inbox `quarantined` với khoá `sha256(raw_body)`, ACK 200, alert; operator `RequeueInbox` sau khi sửa parser |
| DB lỗi trước inbox commit | Không ACK thành công |
| Webhook gửi trùng | Ghi nhận receipt an toàn, không thêm tiền |
| Worker chết sau claim | Lease hết hạn, worker khác claim với generation mới; owner cũ không finalize được |
| Chết sau commit trước publish/ACK | Replay idempotent, sự kiện không mất |
| Tiền ra hoặc chiều tiền `unknown` | Fact vẫn ghi, `not_applicable`, không review (cả nguồn API) |
| Receiver không thuộc binding | Fact ghi với `receiving_account_id NULL`, review `RECEIVER_UNBOUND`; `bind_receiver` + `RematchUnbound` xử lý cả loạt |
| Mã khớp intent của tenant khác | Review `TENANT_MISMATCH` + alert + metric, không settle |
| Không có mã hoặc mã mơ hồ | Lưu unmatched/review, không đoán |
| Lệch tiền/late payment | Review; không đường nào (kể cả operator) settle khi số tiền khác intent |
| Host fulfillment lỗi async | Payment vẫn paid, retry fulfillment |
| Webhook bị provider lọc | Đối soát API, parser trong endpoint không thể cứu |

## Reconciliation và review

Cursor chính là `since_id`; chạy lại cửa sổ có overlap an toàn. API và webhook đi qua cùng `MatchTransaction`, nhưng không giả API receipt có chữ ký webhook. Reader dùng credential API cấp company (`api_credential_ref`) và lọc theo `provider_account_ref`. Observation API `unlinked` được quét lại sau grace dù cursor đã tiến. Mặc định `detect_only`: canonical chỉ có từ API vào review `UNVERIFIED_IDENTITY`, không settle; `auto_settle` chỉ bật khi có bằng chứng hợp lệ. Fact tiền ra/unknown từ API không vào review. Fact chỉ có từ API mà không có thời gian provider đã xác minh không được auto-settle intent đã quá hạn.

Review phải có quyền, actor, reason và audit; một case mở mỗi fact. Cho phép rematch khi intent tới muộn nhưng không tạo hai settlement. Operator không được bypass guard receiver/merchant/tenant, và `attach_to_intent`/`accept_late` chỉ hợp lệ khi đúng số tiền (U9). `mark_duplicate_of` cần provenance có cấu trúc, không dựa vào số tiền/tài khoản/chiều đơn độc.

## Event delivery

Outbox event có event_id, aggregate identifier, type, schema version và trusted scope. Consumer lưu receipt và local side effect cùng transaction nếu có thể. Delivery at-least-once; consumer phải chống trùng. Nếu gọi dịch vụ ngoài cần idempotency contract của dịch vụ đó hoặc phương án recovery riêng, không hứa exactly-once tổng thể.

Các state machine: [PaymentIntent](diagrams/09-state-payment-intent.md), [Inbox/transaction/fulfillment](diagrams/10-state-inbox-transaction.md), [failure sequence](diagrams/07-sequence-failure-retry.md), [reconciliation](diagrams/08-sequence-reconcile.md).
