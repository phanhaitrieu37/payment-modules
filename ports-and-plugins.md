# Ports, plugin và các điểm mở rộng

[← Mục lục](readme.md) · [Class diagram](diagrams/03-ports-classes.md)

## Contract đề xuất

| Port / thành phần | Trách nhiệm |
|---|---|
| PaymentProvider | Verify raw delivery; `extract_event_key(verified)` chỉ đọc trường `id` sau HMAC (int hoặc chuỗi toàn chữ số, thiếu → `None` → inbox `quarantined`); normalize ở worker thành observation; tạo TransferInstruction |
| TransactionReader | Capability đọc giao dịch để đối soát (cursor `since_id`, credential cấp company), không ép mọi provider hỗ trợ |
| ConnectionResolver | Tìm kết nối theo locator, chưa tự tạo trusted merchant context |
| SecretResolver | Resolve secret HMAC webhook theo connection (cửa sổ xoay) và credential API cấp company |
| UnitOfWork và repository | Transaction, locking, persistence và atomic commit; bó repository, không phải god object |
| InvariantGuard | Guard lõi bắt buộc: chiều tiền trước, rồi receiver thuộc binding cùng merchant |
| ReferenceResolver, IntentEligibility | Bước lõi: khớp nguyên reference (tenant khác → `TENANT_MISMATCH`); chặn intent `paid/cancelled/superseded` trước policy |
| MatchingPolicy | Port duy nhất host thay được; chỉ thắt chặt, post-check của lõi từ chối settle lệch tiền |
| PaymentReferenceGenerator | Sinh mã theo profile và tên prefix; DB là nơi quyết định uniqueness |
| ReferenceTemplateChecklist | Validate provider constraints và liệt kê mẫu + bộ lọc cho mỗi prefix có tên, không tự provision |
| SettlementHandler | Host cung cấp hành vi nghiệp vụ, đúng transaction contract đã chọn (phương án A, cùng UoW) |
| OutcomeObserver | Host nhận DTO kết quả bất biến của mỗi fact trong cùng UoW (projection đồng bộ); mặc định no-op |
| OutboxPublisher | Giao sự kiện outbox cho phương án B (`publish(event)`), at-least-once |
| Clock | Nguồn thời gian server, thay được trong test |

## Use case công khai

Không có `PaymentService` gom mọi dependency. Mỗi use case nhận đúng port nó dùng; `build_payment_module(...)` trả một container dataclass chứa các use case đã wire, không có method nghiệp vụ:

- Thanh toán: `CreateIntent` (nhận tên prefix), `GetIntentStatus`, `IngestWebhook`, `ProcessInbox`, `Reconcile`, `DispatchOutbox`.
- Review: `ResolveReview` (resolution `attach_to_intent`, `mark_external`, `mark_duplicate_of`, `bind_receiver`, `accept_late`), `RematchUnbound`, `RequeueInbox`.
- Onboarding: `RegisterMerchant`, `RegisterReceivingAccount`, `RegisterConnection`, `BindConnectionAccount`, `SetConnectionStatus`, `RecordConnectionReadiness`.
- Profile: `CreateReferenceProfile`, `ActivateReferenceProfile`, `RetireReferenceProfile`.

Domain service `MatchTransaction` dùng chung cho ProcessInbox, Reconcile và ResolveReview/RematchUnbound, để webhook, API và operator đi qua cùng chuỗi lõi bằng code chứ không bằng lời. API và chữ ký trong class diagram chỉ là định hướng, chưa phải callable SDK. Các tác vụ thay cấu hình merchant, secret hoặc resolve tiền phải được host kiểm quyền trước khi gọi.

ABC hoặc Protocol cho interface nhỏ. Composition/DI cho phép thay policy/provider/store mà không override quy trình bảo vệ tiền. Không dùng chuỗi inheritance theo project rồi theo merchant, không tạo một lớp cơ sở chứa mọi nghiệp vụ.

Host đăng ký provider và handler tường minh khi startup. Python entry points chỉ có ích khi nhiều distribution provider phát hành độc lập; pluggy khi thật sự có mô hình nhiều hook implementations. Plugin chạy cùng quyền process, không phải sandbox để khách upload code tùy ý.

Policy không được tắt HMAC, thay receiver, bỏ tenant scope hoặc nới dedup. Không mở một callback tổng quát có thể tự commit hoặc sửa fact ngân hàng. Metadata phải có schema/giới hạn, không dùng như escape hatch cho mọi nghiệp vụ.
