# Multi-merchant security và vận hành

[← Mục lục](readme.md) · [Trust boundary](diagrams/05-multi-merchant-trust.md)

## Threat model

Module lưu thông tin nhận tiền, giao dịch và quyết định payment. Failure cần chặn là webhook giả, nhận nhầm merchant, replay tạo thêm quyền lợi, client sửa amount/receiver, rò dữ liệu ngân hàng và xử lý trùng khi retry. Không có tài khoản giữ tiền nền tảng hay payout trong scope.

Host cung cấp authenticated authorization context; module không tin tenant/merchant khách tự khai. Guard kiểm scope, account binding, direction trước business policy. Mọi lookup và FK nghiệp vụ chặn liên kết chéo scope. Prefix project không thay thế tenant isolation.

## Secret và dữ liệu nhạy cảm

SecretResolver lấy secret theo connection từ host secret store hoặc storage mã hóa có key management. Database chỉ giữ secret reference theo đề xuất. Cho phép cửa sổ xoay old/new có giới hạn, không tạo identity tài khoản mới khi đổi credential. Không log secret, raw payload, tài khoản đầy đủ hoặc dữ liệu người chuyển.

Raw body cần để audit/reprocess có access control và retention được chốt trước production. Dữ liệu masked chỉ phục vụ hiển thị; protected canonical account identity dùng để verify. Limit body size và processing limits cần thiết kế cùng router adapter.

## Operational ownership

Host quyết định migrations, worker deployment, secret provisioning và scheduling. Package không tự chạy migration khi import. Backup/restore phải kiểm chứng toàn bộ intent/transaction/settlement/inbox/outbox; không khôi phục một bảng riêng rồi bỏ qua receipt chống trùng.

Metrics đề xuất: webhook auth failures, durable ACK latency, inbox age/retries, unmatched và review backlog, outbox lag, fulfillment failures, reconciliation missing/conflicts, connection/profile readiness. Correlation bằng intent/event ID đã kiểm soát, không dùng raw bank memo.

Provider retry là hữu hạn nên cần alert/reconcile; worker retry có backoff, ngưỡng đưa review/dead-letter và thao tác replay được audit. Lỗi một merchant không chiếm toàn bộ worker hoặc API quota của các merchant khác.

## Giới hạn

Retention, RPO/RTO, production throughput/SLO và chính sách quyền operator chưa được chốt. Tài liệu không cam kết nhận tiền tức thì; đo riêng provider delay và processing delay. Không dùng số liệu restaurant trước đây làm SLA của payment package.
