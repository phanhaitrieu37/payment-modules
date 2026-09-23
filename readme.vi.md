# payment-module

[English](readme.md) · **Tiếng Việt**

Payment module tự vận hành, dùng lại giữa các project Python: thanh toán chuyển khoản ngân hàng
cho nhiều merchant, [SePay](https://sepay.vn) là provider đầu tiên. Người trả chuyển thẳng vào
tài khoản ngân hàng của merchant; module xác nhận, lưu và đối chiếu tiền, còn host giữ giá, đơn
hàng, thuế và quyền lợi.

**Trạng thái:** version `0.1.0`, bản phát hành đầu tiên (import `payment_module`). Bản này có
payment intent kèm hướng dẫn chuyển khoản và payload VietQR, webhook SePay có chữ ký, xử lý
inbox, settle, hàng đợi review, onboarding merchant, đối soát với API SePay, schema PostgreSQL,
FastAPI router tuỳ chọn và worker nền. Giới hạn đã biết và bốn contract có version nằm ở
[CHANGELOG.md](CHANGELOG.md). `v1` trong tên thư mục là phiên bản tài liệu thiết kế, không phải
version package.

## Bảo đảm

- **Chỉ đúng số tiền.** Giao dịch chỉ settle intent khi đúng số tiền; lệch số tiền thì mở review,
  không bao giờ settle một phần hay settle thừa.
- **Cô lập tenant, merchant và environment.** Mọi lookup và composite foreign key đều theo
  tenant, merchant và environment (`test` hoặc `live`); một tài khoản ngân hàng chỉ có một chủ
  trong mỗi environment.
- **Inbox bền trước ACK.** Webhook chỉ được trả 200 sau khi delivery đã commit; delivery trùng
  vẫn được ACK mà không settle lần hai, lỗi database trả 500 để provider gửi lại.
- **Mặc định an toàn.** Đối soát chạy `detect_only`; `auto_settle` cần bằng chứng đã kiểm chứng.

## Cài đặt

Cần Python 3.12+ và PostgreSQL. Cài từ GitHub Release (checksum trong `SHA256SUMS` ở trang
release):

```sh
uv pip install "payment-module[sqlalchemy,postgres,fastapi,sepay] @ https://github.com/phanhaitrieu37/payment-modules/releases/download/v0.1.0/payment_module-0.1.0-py3-none-any.whl"
```

hoặc từ tag git: `payment-module[...] @ git+https://github.com/phanhaitrieu37/payment-modules@v0.1.0`.
Chọn extras theo host; bỏ `fastapi` nếu host không có ứng dụng FastAPI.

## Tài liệu

- [Bắt đầu tích hợp](docs/getting-started.vi.md): từ cài đặt đến một đơn đã thanh toán, từng bước.
- [`examples/saas_host`](examples/saas_host): FastAPI, settle trong transaction của host.
- [`examples/fnb_host`](examples/fnb_host): không web framework, consumer outbox.
- [CHANGELOG.md](CHANGELOG.md): contract, chính sách version và giới hạn đã biết.
- Tài liệu thiết kế: xem [Thứ tự đọc](#thứ-tự-đọc) bên dưới.

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

Các tên class, bảng, cấu hình và signature trong tài liệu thiết kế là ngôn ngữ thiết kế, chưa phải API ổn định; API hiện hành là code trong `src/` và [hướng dẫn tích hợp](docs/getting-started.vi.md). Không dùng tài liệu để tuyên bố một khả năng đã triển khai. Quyết định trực tiếp của người dùng ưu tiên; chi tiết chưa chốt nằm trong sổ câu hỏi mở. Sơ đồ nguồn được bảo toàn, các giới hạn cần làm rõ trước implementation được ghi trong data-model và validation.
