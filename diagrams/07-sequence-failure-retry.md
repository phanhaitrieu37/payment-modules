# D07 — Sequence: lỗi, trùng và retry

[← Mục lục](../readme.md) · [Danh mục sơ đồ](../diagrams.md)

Trạng thái: sơ đồ kiến trúc đề xuất; không phải bằng chứng triển khai. Mermaid đã sửa phạm vi dedup sau lần render 21/09/2026, chưa render lại.

Bảy tình huống: sai chữ ký, DB lỗi trước khi inbox commit, gửi trùng, worker chết, không khớp chính xác, lỗi tạm khi xử lý, và fulfillment của host lỗi.

- Retry của SePay là hữu hạn; hệ thống không được dựa vào retry vô hạn.
- Ràng buộc unique trong DB — không phải lock trong bộ nhớ — chặn ghi tiền hai lần.
- Lệch tiền hay trả muộn không bao giờ tự settle: ghi fact, mở ReviewCase, phát NeedsReview. Giao dịch unmatched được lưu bền và rematch được.
- Fulfillment của host lỗi không đảo trạng thái paid; host retry idempotent theo event_id.

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
  participant S as SePay
  participant R as Webhook endpoint
  participant DB as PostgreSQL
  participant W as Worker (inbox + outbox dispatcher)
  participant O as Operator (review)
  participant HH as Host fulfillment
  rect rgb(255, 228, 230)
    Note over S,R: A. Không xác thực được
    S->>R: Webhook sai chữ ký / timestamp quá hạn
    R-->>S: 401, không ghi inbox
  end
  rect rgb(255, 237, 213)
    Note over S,DB: B. DB lỗi trước khi inbox commit
    S->>R: Webhook hợp lệ
    R-xDB: INSERT inbox thất bại (DB tạm lỗi)
    R-->>S: 5xx, không ACK
    Note over S: SePay retry theo lịch hữu hạn (~1 + 7 lần), không vô hạn
    S->>R: Retry
    R->>DB: INSERT inbox, COMMIT
    R-->>S: 200
  end
  rect rgb(241, 245, 249)
    Note over S,DB: C. Gửi trùng / đồng thời
    S->>R: Cùng giao dịch lần 2
    R->>DB: INSERT inbox → vi phạm unique(connection, event_key)
    R-->>S: 200, không tạo tác dụng mới
  end
  rect rgb(254, 243, 199)
    Note over W,DB: D. Worker chết giữa chừng
    W->>DB: Claim inbox (lease)
    Note over W: Crash trước COMMIT → DB rollback toàn bộ
    W->>DB: Worker khác claim lại khi lease hết hạn
    W->>DB: Unique (tenant, environment, dedup_key) + unique Settlement chặn ghi tiền hai lần
  end
  rect rgb(255, 228, 230)
    Note over W,O: E. Không khớp chính xác
    W->>DB: Ghi ProviderTransaction (fact ngân hàng vẫn được lưu)
    W->>DB: Sai số tiền / intent hết hạn / không có mã → ReviewCase + Outbox PaymentNeedsReview
    Note over W,DB: Giao dịch unmatched được lưu bền và rematch được (intent tới muộn, operator gán)
    O->>DB: Quyết định thủ công (chấp nhận muộn → Settlement origin=operator_review, hoặc xử lý ngoài hệ thống)
  end
  rect rgb(254, 215, 170)
    Note over W,DB: F. Lỗi tạm khi xử lý
    W->>DB: inbox → retry_wait, attempts+1, next_attempt_at (backoff)
    W->>DB: Quá ngưỡng → failed, cảnh báo, operator requeue
  end
  rect rgb(224, 231, 255)
    Note over W,HH: G. Fulfillment của host lỗi (phương án outbox)
    W->>HH: PaymentSettled (at-least-once)
    Note over HH: Cấp quyền / đóng bill thất bại → fulfillment = failed. Payment VẪN paid
    W->>HH: Gửi lại PaymentSettled
    Note over HH: Receipt(event_id) đã có → bỏ qua, chưa có → thực hiện, fulfillment = done
    HH-->>W: Xác nhận đã xử lý event_id
  end
```
