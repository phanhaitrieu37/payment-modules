# Ports, plugin và các điểm mở rộng

[← Mục lục](readme.md) · [Class diagram](diagrams/03-ports-classes.md)

## Contract đề xuất

| Port / thành phần | Trách nhiệm |
|---|---|
| PaymentProvider | Verify raw delivery, normalize provider data, tạo TransferInstruction |
| TransactionReader | Capability đọc giao dịch để đối soát, không ép mọi provider hỗ trợ |
| ConnectionResolver | Tìm kết nối theo locator, chưa tự tạo trusted merchant context |
| SecretResolver | Resolve secret theo connection, hỗ trợ cửa sổ xoay |
| UnitOfWork và repository | Transaction, locking, persistence và atomic commit |
| InvariantGuard | Guard lõi bắt buộc về receiver, tenant và chiều tiền |
| MatchingPolicy | Quyết định settle hoặc review sau guard/dedup |
| PaymentReferenceGenerator | Sinh mã theo profile; DB là nơi quyết định uniqueness |
| ReferenceTemplateChecklist | Validate provider constraints và hướng dẫn cấu hình, không tự provision |
| SettlementHandler | Host cung cấp hành vi nghiệp vụ, đúng transaction contract đã chọn |

## Use case công khai

Tạo/đọc yêu cầu thanh toán; nhận webhook qua router adapter; xử lý inbox; chạy đối soát; đọc trạng thái; xử lý review có quyền và audit. API và chữ ký trong class diagram chỉ là định hướng, chưa phải callable SDK. Các tác vụ thay cấu hình merchant, secret hoặc resolve tiền phải được host kiểm quyền trước khi gọi.

ABC hoặc Protocol cho interface nhỏ. Composition/DI cho phép thay policy/provider/store mà không override quy trình bảo vệ tiền. Không dùng chuỗi inheritance theo project rồi theo merchant, không tạo một lớp cơ sở chứa mọi nghiệp vụ.

Host đăng ký provider và handler tường minh khi startup. Python entry points chỉ có ích khi nhiều distribution provider phát hành độc lập; pluggy khi thật sự có mô hình nhiều hook implementations. Plugin chạy cùng quyền process, không phải sandbox để khách upload code tùy ý.

Policy không được tắt HMAC, thay receiver, bỏ tenant scope hoặc nới dedup. Không mở một callback tổng quát có thể tự commit hoặc sửa fact ngân hàng. Metadata phải có schema/giới hạn, không dùng như escape hatch cho mọi nghiệp vụ.
