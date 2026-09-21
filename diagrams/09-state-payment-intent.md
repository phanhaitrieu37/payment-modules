# D09 — Vòng đời PaymentIntent

[← Mục lục](../readme.md) · [Danh mục sơ đồ](../diagrams.md)

Trạng thái: sơ đồ kiến trúc đề xuất; không phải bằng chứng triển khai. Nguồn Mermaid được chuyển nguyên từ bản thiết kế đã render/kiểm tra ngày 21/09/2026.

Intent chỉ tới paid qua Settlement đã commit. Đổi số tiền tạo intent mới; intent cũ superseded.

- Chấp nhận tiền muộn là quyết định của operator qua ReviewCase, không tự động.
- Hoàn tiền và thanh toán một phần chưa được chốt nên không có trạng thái tương ứng.

```mermaid
---
config:
  theme: base
  fontFamily: Arial, Helvetica, sans-serif
  themeVariables:
    fontFamily: "Arial, Helvetica, sans-serif"
    fontSize: 15px
---
stateDiagram-v2
  direction TB
  [*] --> awaiting_payment: create_intent (amount + beneficiary snapshot bất biến)
  awaiting_payment --> paid: Settlement tự động (khớp chính xác)
  awaiting_payment --> expired: quá expires_at
  awaiting_payment --> cancelled: host huỷ đơn
  awaiting_payment --> superseded: host đổi số tiền → tạo intent mới
  expired --> paid: operator chấp nhận tiền muộn (ReviewCase)
  paid --> [*]
  cancelled --> [*]
  superseded --> [*]
  expired --> [*]
  note right of awaiting_payment
    Tạo QR / browser quay lại
    không đổi trạng thái
  end note
  note right of paid
    Chỉ đến đây qua Settlement đã commit.
    Hoàn tiền: CHƯA chốt, không có trạng thái
  end note
```
