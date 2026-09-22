# Kiểm chứng và tiêu chí nghiệm thu

[← Mục lục](readme.md)

Đây là test plan và acceptance criteria của thiết kế, không phải kết quả kiểm thử module đã triển khai.

## Checklist bàn giao implementation

- [ ] Chốt model, composite uniqueness/FK, account binding và scope project/tenant.
- [ ] Chốt API, error shapes, event schema, UnitOfWork và handler modes.
- [ ] Xác minh SePay webhook/API identity trên dữ liệu test có quyền sử dụng.
- [ ] Chốt profile generator, độ dài suffix, onboarding và rotation.
- [ ] Xây provider/storage/FastAPI adapters, inbox/outbox và reconciliation.
- [ ] Hai host mẫu (SaaS/F&B) dùng cùng package version không sửa vendor; tích hợp MeowAI là plan riêng.
- [ ] Kiểm thử concurrency trên PostgreSQL, migration/restore và failure recovery.

## Acceptance cases

| Ca | Kết quả cần quan sát |
|---|---|
| Reuse | Hai project cài cùng package version, không sửa vendor code |
| Merchant isolation | Mọi ca chéo tenant, chéo merchant cùng tenant, chéo Test/Live và tài khoản đã có chủ trong environment đều bị DB từ chối hoặc không settle |
| Create idempotency | Cùng key/payload trả cùng intent; payload khác báo conflict |
| Concurrent webhook replay | Gửi cùng giao dịch 100 lần kể cả đồng thời: một fact logic, một settlement/fulfillment |
| API + webhook | Cùng giao dịch qua hai nguồn ở hai thứ tự không nhân tiền: webhook trùng ×3 + đối soát chồng lấn ×2 → đúng 1 observation webhook + 1 observation API, 1 provider transaction, 1 settlement (D4) |
| Durable ACK | Không có success ACK nếu inbox chưa commit |
| Crash recovery | Kill sau commit trước publish/ACK vẫn replay được, không lặp tác dụng |
| Mismatch/late/ambiguous | Fact còn trong review, không tự settle |
| Receiver spoof/outgoing | Không settle dù mã và amount trùng intent |
| Reference uniqueness | Ép collision generator, DB từ chối và retry theo giới hạn |
| Prefix/provider config | Test min/max, charset, case, template order, code filter và Test/Live separation |
| Rotation | Mã cũ còn xử lý được; code và beneficiary snapshot không đổi |
| Host async failure | Payment paid, fulfillment failed/pending và retry idempotent |
| Migration/restore | Nâng cấp/restore không mất ràng buộc hoặc nhân bản tiền |

Test tự động chứng minh từng ca và từng bất biến được liệt kê trong `tests/integration/acceptance/traceability.py`; `make acceptance` chạy bộ acceptance và kiểm bảng ánh xạ đó còn khớp.

Provider boundary cần thêm real-bank sample được cho phép, không coi sandbox payload đủ chứng minh mọi cách ngân hàng biến đổi memo.

## Verification của bộ tài liệu gốc

Sơ đồ nguồn đã render bằng mermaid-cli 11.12.0 ngày 21/09/2026; nguồn/render hashes được kiểm tra sau cập nhật prefix. Từ 22/09/2026 các sơ đồ D02, D03, D04, D05, D06, D07, D08, D10, D11 và D13 đã sửa tay theo delta và **chưa render lại**; bản render cũ không còn khớp nguồn. Link public đã có 13 SVG ở lần xác minh 21/09/2026. Kiểm tra link nội bộ và tính đầy đủ của Markdown là kiểm chứng tài liệu, không phải test payment.

Các nhược điểm hiển thị nguồn: sequence lỗi có lifeline kết thúc trước note cuối; component diagram rộng cần phóng to; chưa thử thiết bị mobile thật. Scope proposal và câu hỏi chưa chốt vẫn được hiển thị, không biến coverage thành phần trăm hoàn thành code.
