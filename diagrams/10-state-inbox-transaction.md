# D10 — Vòng đời Inbox và kết quả matching

[← Mục lục](../readme.md) · [Danh mục sơ đồ](../diagrams.md)

Trạng thái: sơ đồ kiến trúc đề xuất; không phải bằng chứng triển khai. Nguồn Mermaid được chuyển nguyên từ bản thiết kế đã render/kiểm tra ngày 21/09/2026.

Ba lớp tách biệt: xử lý bản tin (WebhookInbox), kết quả matching của giao dịch ngân hàng đã xác thực, và fulfillment nghiệp vụ của host.

- Lease hết hạn đưa bản tin về received để worker khác xử lý lại.
- Fact ngân hàng (recorded) không bị xoá hay sửa khi kết quả matching thay đổi.
- Giao dịch in_review được rematch khi intent tới muộn hoặc operator gán.
- Fulfillment failed chỉ là trạng thái của host; intent vẫn paid.

```mermaid
---
config:
  theme: base
  layout: elk
  fontFamily: Arial, Helvetica, sans-serif
  themeVariables:
    fontFamily: "Arial, Helvetica, sans-serif"
    fontSize: 15px
---
stateDiagram-v2
  direction LR
  state "Lớp 1 — xử lý bản tin WebhookInbox" as INBOX {
    [*] --> received: verify OK + commit → ACK
    received --> processing: worker claim (lease)
    processing --> processed: commit fact + quyết định
    processing --> retry_wait: lỗi tạm thời
    retry_wait --> processing: tới next_attempt_at
    processing --> received: lease hết hạn (worker chết)
    retry_wait --> failed: vượt số lần thử
    failed --> received: operator requeue
    processing --> ignored: giao dịch tiền ra / không liên quan
    processed --> [*]
    ignored --> [*]
  }
  state "Lớp 2 — matching của ProviderTransaction" as MATCH {
    [*] --> recorded: fact ngân hàng bất biến
    recorded --> settled: SETTLE (Settlement)
    recorded --> in_review: REVIEW_*
    in_review --> settled: rematch (intent tới muộn) / operator gán
    in_review --> closed_external: xử lý ngoài hệ thống
    settled --> [*]
    closed_external --> [*]
  }
  state "Lớp 3 — fulfillment của host (phương án outbox)" as FUL {
    [*] --> pending: PaymentSettled đã commit
    pending --> done: host xử lý xong (receipt event_id)
    state "failed (intent vẫn paid)" as ffailed
    pending --> ffailed: lỗi nghiệp vụ host
    ffailed --> pending: retry idempotent
    done --> [*]
  }
```
