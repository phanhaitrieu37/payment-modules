# Domain và mô hình dữ liệu

[← Mục lục](readme.md) · [ER diagram](diagrams/04-entity-relationship.md)

## Khái niệm

| Entity | Ý nghĩa và ràng buộc |
|---|---|
| Tenant | Scope của host, không cần module sở hữu vòng đời user/tenant |
| Merchant | Đơn vị nhận tiền, có host reference và trạng thái |
| ReceivingAccount | Tài khoản ngân hàng/VA của merchant, identity ổn định |
| ProviderConnection | Cấu hình provider, locator và tham chiếu secret, binding tài khoản |
| ReferenceProfile | Mẫu sinh mã cấp project có phiên bản |
| ConnectionReferenceReadiness | Bằng chứng thủ công cấu hình SePay phù hợp một profile |
| PaymentIntent | Amount integer VND, beneficiary snapshot, merchant, reference, expiry và business reference |
| WebhookInbox | Raw delivery đã xác thực, processing state, lease, attempts và retry |
| ProviderTransaction | Fact ngân hàng, amount, direction, identity và nguồn quan sát |
| Settlement | Liên kết khoản tiền với intent, thời điểm và nguồn quyết định |
| ReviewCase | Khoản tiền cần kiểm tra, lý do, người xử lý và dấu vết quyết định |
| OutboxEvent | Sự kiện đã commit cùng thay đổi domain, chờ delivery |
| ReconciliationRun | Cửa sổ đối soát, cursor và trạng thái lần chạy |

Tên và số bảng là đề xuất thiết kế; không phải schema triển khai cuối cùng.

## Quy tắc về tiền và identity

Amount VND là số nguyên, không dùng float để tính/so khớp. Intent snapshot số tiền, người thụ hưởng, mã và profile version bất biến. Thay số tiền tạo intent mới; không sửa lịch sử để làm cho giao dịch khớp. Cùng idempotency key với payload khác trả conflict, không trả nhầm intent cũ.

Mặc định một giao dịch đúng tiền tất toán một intent. Unique/lock tại database bảo đảm không hai worker cùng settle. Ràng buộc khóa ngoại phải chặn liên kết chéo tenant, không chỉ dựa vào filter ứng dụng. Phạm vi dedup dựa trên provider account ổn định, không dựa vào secret hoặc connection ID có thể đổi.

Identity webhook và API v2 chưa được chứng minh tương đương. Giữ identifiers riêng và chỉ hợp nhất khi có chứng cứ. Không sử dụng amount+thời gian gần nhau như bằng chứng duy nhất cùng giao dịch.

## Ba loại trạng thái độc lập

1. Bản tin đã được tiếp nhận/xử lý.
2. Giao dịch đã được ghi nhận và khớp với intent hay cần review.
3. Quyền lợi của host đã được giao thành công hay đang retry.

Giao dịch unmatched vẫn tồn tại. Trong phương án async, host fulfillment lỗi không làm intent đã paid thành pending. Trong phương án cùng UoW, lỗi handler làm rollback cả quyết định settlement của transaction đó; inbox đã tiếp nhận vẫn còn để retry.

## Giới hạn sơ đồ phải giải quyết trước khi tạo schema

- ER hiện minh hoạ một connection theo dõi một receiving account. Nếu một SePay company/webhook theo dõi nhiều tài khoản thì adapter phải biểu diễn tập binding rõ ràng; không mặc định payload thuộc tài khoản duy nhất theo locator.
- Quan hệ inbox–transaction vẽ đơn giản không đủ cho nhiều delivery/connection quan sát cùng fact. Bản schema thật phải bảo toàn provenance nhiều quan sát cùng một giao dịch mà không nhân đôi tiền.
- Các trường `*_masked` chỉ dùng hiển thị. Giá trị che một phần không đủ để tạo QR hoặc đối chiếu chính xác; implementation phải có nguồn dữ liệu đầy đủ được bảo vệ và canonical identity/fingerprint phù hợp.
- ReferenceProfile là cấu hình cấp project, khác với bảng dữ liệu tenant. Không áp quy tắc “mọi bảng đều tenant-scoped” máy móc vào cấu hình project. Readiness kế thừa scope connection; cần định nghĩa composite constraints tương ứng.
- Reference unique toàn project là hướng đề xuất. Database cục bộ không bảo đảm unique giữa các project độc lập dùng chung tài khoản.

Các điểm này là giới hạn cần hoàn thiện, không phải chức năng đã có. Xem [câu hỏi mở](decisions-and-open-questions.md).
