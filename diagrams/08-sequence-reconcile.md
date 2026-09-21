# D08 — Sequence: đối soát qua API

[← Mục lục](../readme.md) · [Danh mục sơ đồ](../diagrams.md)

Trạng thái: sơ đồ kiến trúc đề xuất; không phải bằng chứng triển khai. Nguồn Mermaid được chuyển nguyên từ bản thiết kế đã render/kiểm tra ngày 21/09/2026.

Bù webhook bị lỡ bằng cửa sổ thời gian có chồng lấn và cursor. Giao dịch không chứng minh được là mới hay trùng thì vào review thay vì tự settle.

- Webhook dùng ID số; API v2 dùng UUID — cần kiểm chứng bằng tài khoản test trước khi tự động hợp nhất.
- Chạy lại một cửa sổ đã đối soát phải an toàn (idempotent).

```mermaid
---
config:
  theme: base
  fontFamily: Arial, Helvetica, sans-serif
  sequence:
    wrap: true
    width: 190
    messageFontSize: 15
    noteFontSize: 14
    actorFontSize: 15
    mirrorActors: false
    showSequenceNumbers: true
  themeVariables:
    fontFamily: "Arial, Helvetica, sans-serif"
---
sequenceDiagram
  autonumber
  participant J as Scheduler
  participant RC as Reconcile use case
  participant TR as SePay reader (adapter)
  participant S as SePay API
  participant DB as PostgreSQL
  participant O as Operator
  J->>RC: reconcile(connection, window có chồng lấn)
  RC->>DB: Tạo ReconciliationRun (cursor)
  loop Mỗi trang
    RC->>TR: list_transactions(connection, window, cursor)
    TR->>S: Gọi API giao dịch (credential của merchant)
    S-->>TR: Danh sách giao dịch (định danh dạng UUID trong API v2)
    TR-->>RC: NormalizedTransaction[]
    RC->>DB: Tìm fact đã có theo định danh ĐÃ xác minh
    alt Đã có (cùng giao dịch, định danh xác minh)
      Note over RC: Bỏ qua — không tạo thêm tiền
    else Chưa có và định danh liên kết được chắc chắn
      RC->>DB: INSERT ProviderTransaction (first_source=reconcile) rồi matching như webhook
    else Không chứng minh được là giao dịch mới hay trùng
      RC->>DB: ReviewCase(reason=unverified_identity), KHÔNG tự settle
      O->>DB: Operator đối chiếu và quyết định
    end
    RC->>DB: Lưu cursor
  end
  RC->>DB: Đóng run, ghi số liệu (mới, bỏ qua, review)
  Note over RC,S: Ánh xạ ID số webhook ↔ UUID API v2 CHƯA xác minh — phải kiểm chứng bằng tài khoản test trước khi tự động hoá hợp nhất
```
