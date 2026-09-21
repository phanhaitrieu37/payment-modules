# Payment Modules V1 — Architecture Design

Bộ tài liệu thiết kế Payment module dùng lại giữa các project Python, tích hợp SePay, hỗ trợ nhiều đơn vị nhận tiền và prefix mã thanh toán cấu hình theo project.

**Trạng thái:** thiết kế đề xuất, chưa có package chạy được, chưa có migration hoặc integration test của module mới. Bộ tài liệu tổng hợp các quyết định và phân tích tới ngày 21/09/2026; `v1` là phiên bản tài liệu, không phải release phần mềm.

## Mục tiêu và phạm vi

Mỗi project cài module và tự vận hành. Khách thanh toán thẳng vào tài khoản của merchant. Module xác nhận, lưu và đối chiếu tiền; host sở hữu giá, đơn hàng, thuế và quyền lợi. SePay là provider ban đầu; kiến trúc mở rộng bằng interface nhỏ và composition/DI.

Restaurant và MeowAI chỉ là ví dụ tích hợp. Không thiết kế restaurant offline/LAN/bếp, không thu hộ, chia tiền, payout, không đưa VAT/e-invoice/subscription/credit vào lõi.

## Thứ tự đọc

| Tài liệu | Nội dung |
|---|---|
| [Architecture overview](architecture-overview.md) | Ranh giới module, deployment, stack và đánh đổi |
| [Architecture design](architecture-design.md) | Bản chụp đầy đủ thiết kế đã phân tích và cập nhật prefix |
| [Domain và dữ liệu](data-model.md) | Tenant, merchant, intent, giao dịch, settlement và ràng buộc |
| [Ports và plugin](ports-and-plugins.md) | OOP, use case, dependency direction, public surface |
| [Provider SePay](sepay-integration.md) | Webhook, xác thực, lọc và reconciliation |
| [Payment reference](payment-reference.md) | Prefix project, suffix, profile version, onboarding và rotation |
| [Processing và recovery](processing-and-recovery.md) | Transaction, inbox/outbox, retry, review và trạng thái |
| [Security và vận hành](security-and-operations.md) | Multi-merchant isolation, secret, monitoring và retention |
| [Tích hợp và phát hành](integration-and-versioning.md) | Host handlers, packaging, migrations và lộ trình tách MeowAI |
| [Validation](validation-and-acceptance.md) | Checklist và tiêu chí nghiệm thu để triển khai tiếp |
| [Quyết định và câu hỏi mở](decisions-and-open-questions.md) | Đã chốt, đề xuất, trade-offs, điều kiện xem xét lại |
| [Sơ đồ](diagrams.md) | Mermaid architecture, ER, sequence, state và pipeline |
| [Bằng chứng](evidence-matrix.md) | Ma trận nguồn và độ bao phủ |
| [Nguồn tham khảo](sources.md) | URL nguồn đã đọc, độ tin cậy và giới hạn nghiên cứu |

## Xem trực quan

[Bản Orca Artifacts](https://share.onorca.dev/a/uocuGxDkRWhN) được cập nhật cùng thiết kế trước khi xuất bộ Markdown này. Link được ghi nhận hết hạn 21/10/2026. Bộ Markdown và Mermaid tại thư mục này không phụ thuộc link public hoặc checkout MeowAI để đọc.

## Quy tắc sử dụng

Các tên class, bảng, cấu hình và signature là ngôn ngữ thiết kế, chưa phải API ổn định. Không dùng tài liệu để tuyên bố một khả năng đã triển khai. Quyết định trực tiếp của người dùng ưu tiên; chi tiết chưa chốt nằm trong sổ câu hỏi mở. Sơ đồ nguồn được bảo toàn, các giới hạn cần làm rõ trước implementation được ghi trong data-model và validation.
