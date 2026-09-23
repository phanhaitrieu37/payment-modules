# Payment Modules V1 — Architecture Design

Bộ tài liệu thiết kế Payment module dùng lại giữa các project Python, tích hợp SePay, hỗ trợ nhiều đơn vị nhận tiền và prefix mã thanh toán cấu hình theo project.

**Trạng thái:** package `payment-module` (import `payment_module`) phát hành bản `0.1.0` (local-only: tag git `v0.1.0` + wheel build tái lập, không PyPI). Bản này có đủ luồng tạo intent, nhận webhook SePay, xử lý inbox, settle, review, onboarding, đối soát API v2, schema PostgreSQL `schema_v1`, FastAPI router tuỳ chọn và worker; mặc định an toàn `reconcile_mode = detect_only`, cửa sổ timestamp 300 giây. Giới hạn đã biết và thay đổi theo bốn contract ở [CHANGELOG](CHANGELOG.md). `v1` trong tên thư mục là phiên bản tài liệu thiết kế, không phải version package.

## Cài đặt

```sh
uv pip install "payment_module-0.1.0-py3-none-any.whl[sqlalchemy,postgres,fastapi,sepay]"
```

Chọn extras theo host (`fastapi` chỉ khi dùng router). Hướng dẫn tích hợp, chính sách version và cách pin ở [Tích hợp và phát hành](integration-and-versioning.md); hai host mẫu cài cùng wheel: [`examples/saas_host`](examples/saas_host) (FastAPI, handler cùng UnitOfWork) và [`examples/fnb_host`](examples/fnb_host) (không FastAPI, consumer outbox).

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
| [Tích hợp và phát hành](integration-and-versioning.md) | Host handlers, packaging, migrations và quan hệ tham chiếu với MeowAI |
| [Validation](validation-and-acceptance.md) | Checklist và tiêu chí nghiệm thu để triển khai tiếp |
| [Quyết định và câu hỏi mở](decisions-and-open-questions.md) | Đã chốt, đề xuất, trade-offs, điều kiện xem xét lại |
| [Sơ đồ](diagrams.md) | Mermaid architecture, ER, sequence, state và pipeline |
| [Bằng chứng](evidence-matrix.md) | Ma trận nguồn và độ bao phủ |
| [Nguồn tham khảo](sources.md) | URL nguồn đã đọc, độ tin cậy và giới hạn nghiên cứu |

## Xem trực quan

[Bản Orca Artifacts](https://share.onorca.dev/a/uocuGxDkRWhN) được cập nhật cùng thiết kế trước khi xuất bộ Markdown này. Link được ghi nhận hết hạn 21/10/2026. Bộ Markdown và Mermaid tại thư mục này không phụ thuộc link public hoặc checkout MeowAI để đọc.

## Quy tắc sử dụng

Các tên class, bảng, cấu hình và signature là ngôn ngữ thiết kế, chưa phải API ổn định. Không dùng tài liệu để tuyên bố một khả năng đã triển khai. Quyết định trực tiếp của người dùng ưu tiên; chi tiết chưa chốt nằm trong sổ câu hỏi mở. Sơ đồ nguồn được bảo toàn, các giới hạn cần làm rõ trước implementation được ghi trong data-model và validation.
