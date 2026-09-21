# D06 — Sequence: thanh toán thành công

[← Mục lục](../readme.md) · [Danh mục sơ đồ](../diagrams.md)

Trạng thái: sơ đồ kiến trúc đề xuất; không phải bằng chứng triển khai. Mermaid đã sửa tên participant và bước fact sau lần render 21/09/2026, chưa render lại.

Từ checkout tới khi host hiển thị “Đã thanh toán”. ACK cho SePay chỉ sau khi inbox đã commit; tiền, trạng thái intent, outbox và (nếu chọn) quyền lợi của host commit cùng một transaction.

- Mã tham chiếu sinh từ profile đang active của project. SePay trích code theo mẫu cấp công ty của merchant; server vẫn khớp nguyên reference sau HMAC, receiver và dedup.
- Tạo QR hay browser quay lại không bao giờ đổi trạng thái sang đã trả.
- Worker xử lý tách khỏi request webhook, nên ACK không phụ thuộc thời gian xử lý nghiệp vụ.
- Bước on_settled chỉ áp dụng cho phương án handler cùng UoW (xem D12).

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
  actor C as Khách hàng
  participant H as Host app (giá, đơn)
  participant P as Payment module (CreateIntent, IngestWebhook)
  participant DB as PostgreSQL (UoW)
  participant BK as Ngân hàng merchant
  participant S as SePay
  participant W as Inbox worker
  participant HH as SettlementHandler (host)
  C->>H: Checkout
  Note right of H: Host tự tính giá, thuế, tạo đơn
  H->>P: create_intent(tenant, merchant, amount, prefix_name, host_ref, idempotency_key)
  Note over P: ReferenceGenerator dùng profile active của project: prefix theo tên host chọn + hậu tố ngẫu nhiên (unique DB, retry khi trùng)
  P->>DB: INSERT PaymentIntent + beneficiary snapshot + payment_reference + profile_version + prefix_name
  P-->>H: TransferInstruction (VietQR, số TK, nội dung)
  H-->>C: Hiện QR / chỉ dẫn — trạng thái "chờ thanh toán"
  Note over C,H: Tạo QR hay browser quay lại KHÔNG đánh dấu đã trả
  C->>BK: Chuyển khoản đúng số tiền + mã tham chiếu
  BK->>S: Biến động số dư
  Note over S: Trích code theo mẫu cấp công ty của merchant (mẫu khớp đầu tiên), bộ lọc webhook theo code/tiền tố
  S->>P: POST webhook /{locator} (HMAC, timestamp, raw body)
  Note over P: Tra connection theo locator → verify HMAC trên raw body → mới tin merchant context
  P->>DB: INSERT WebhookInbox (unique event_key), COMMIT
  P-->>S: 200 {"success": true} sau khi inbox đã commit
  W->>DB: Claim inbox (FOR UPDATE SKIP LOCKED, lease)
  Note over W: Normalize payload (sau khi đã verify)
  rect rgb(236, 253, 245)
    Note over DB,HH: Một transaction DB (cùng database)
    W->>DB: INSERT ProviderObservation + ProviderTransaction canonical (unique dedup_key)
    Note over W: Guard: tiền vào, receiver trong payload thuộc binding của connection (cùng merchant)
    W->>DB: Tìm intent theo NGUYÊN reference trong project, scope tenant + tài khoản nhận
    Note over W: Không có / nhiều ứng viên → review, không đoán
    Note over W: IntentEligibility → ExactAmountPolicy → SETTLE → post-check đúng số tiền
    W->>DB: INSERT Settlement, intent → paid, OutboxEvent PaymentSettled
    W->>HH: on_settled(uow, settlement)
    Note right of HH: Host cấp quyền / đóng bill trong cùng UoW, không commit riêng, không gọi API ngoài
    W->>DB: inbox → processed, COMMIT
  end
  C->>H: Poll / SSE trạng thái đơn
  H->>DB: Đọc trạng thái intent/đơn
  H-->>C: "Đã thanh toán" (chỉ khi settlement đã commit)
```
