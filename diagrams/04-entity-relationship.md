# D04 — Mô hình thực thể (ER)

[← Mục lục](../readme.md) · [Danh mục sơ đồ](../diagrams.md)

Trạng thái: sơ đồ kiến trúc đề xuất; không phải bằng chứng triển khai. Nguồn Mermaid được chuyển nguyên từ bản thiết kế đã render/kiểm tra ngày 21/09/2026.

Mọi bảng mang tenant_id. Intent bất biến về merchant, tài khoản thụ hưởng và số tiền; fact ngân hàng tách khỏi kết quả matching; Settlement là cầu nối duy nhất giữa tiền và intent.

- dedup_key dựa trên định danh tài khoản ổn định (provider_account_key), không dựa vào connection — xoay credential hay tạo lại connection không sinh bản ghi tiền mới.
- webhook_tx_id và api_tx_id là hai cột riêng; ánh xạ giữa chúng CHƯA xác minh.
- Unique trên Settlement.transaction_id và Settlement.intent_id thể hiện mặc định 1 giao dịch ↔ 1 intent đúng tiền.
- REFERENCE_PROFILE thuộc project (mỗi project một database). Intent snapshot reference và profile_version, không bao giờ rewrite. CONNECTION_REFERENCE_READINESS lưu checklist onboarding và bằng chứng xác minh thủ công của từng connection.
- Các unique ghi trên sơ đồ là ý đồ ràng buộc; trong schema thật chúng là composite có tenant_id.

```mermaid
---
config:
  theme: base
  fontFamily: Arial, Helvetica, sans-serif
  themeVariables:
    fontFamily: "Arial, Helvetica, sans-serif"
    fontSize: 15px
---
erDiagram
  REFERENCE_PROFILE ||--o{ PAYMENT_INTENT : "sinh mã (snapshot version)"
  REFERENCE_PROFILE ||--o{ CONNECTION_REFERENCE_READINESS : "áp dụng"
  PROVIDER_CONNECTION ||--o{ CONNECTION_REFERENCE_READINESS : "checklist"
  MERCHANT ||--o{ PROVIDER_CONNECTION : "có"
  MERCHANT ||--o{ RECEIVING_ACCOUNT : "sở hữu"
  PROVIDER_CONNECTION }o--|| RECEIVING_ACCOUNT : "theo dõi"
  MERCHANT ||--o{ PAYMENT_INTENT : "thu tiền"
  RECEIVING_ACCOUNT ||--o{ PAYMENT_INTENT : "snapshot người thụ hưởng"
  PROVIDER_CONNECTION ||--o{ WEBHOOK_INBOX : "nhận qua locator"
  RECEIVING_ACCOUNT ||--o{ PROVIDER_TRANSACTION : "tiền vào"
  WEBHOOK_INBOX |o--o| PROVIDER_TRANSACTION : "chuẩn hoá thành"
  PROVIDER_TRANSACTION ||--o| SETTLEMENT : "phân bổ 1-1"
  PAYMENT_INTENT ||--o| SETTLEMENT : "tất toán"
  PROVIDER_TRANSACTION ||--o{ REVIEW_CASE : "cần xem xét"
  PROVIDER_CONNECTION ||--o{ RECONCILIATION_RUN : "đối soát"
  SETTLEMENT ||--o{ OUTBOX_EVENT : "phát sự kiện"
  REVIEW_CASE ||--o{ OUTBOX_EVENT : "phát sự kiện"

  REFERENCE_PROFILE {
    int version PK "bất biến sau khi dùng"
    string prefix "theo project, A-Z, 2-5"
    int suffix_length "cố định; 12 chỉ là ví dụ"
    string alphabet "A-Z0-9 hoặc 0-9"
    string status "draft, active, accepted_legacy"
  }
  CONNECTION_REFERENCE_READINESS {
    uuid connection_id FK
    int profile_version FK
    string status "pending, ready, retired"
    json checklist "mẫu công ty, binding, bộ lọc webhook"
    string verified_by "xác minh thủ công"
    timestamptz verified_at
  }
  MERCHANT {
    uuid id PK
    string tenant_id "scope do host cấp"
    string host_merchant_ref "tham chiếu nghiệp vụ host"
    string status
  }
  RECEIVING_ACCOUNT {
    uuid id PK
    string tenant_id
    uuid merchant_id FK
    string bank_code
    string account_number_masked
    string account_fingerprint UK "định danh ổn định, tenant+bank+số TK"
    string holder_name
  }
  PROVIDER_CONNECTION {
    uuid id PK
    string tenant_id
    uuid merchant_id FK
    uuid receiving_account_id FK
    string provider "sepay"
    string locator UK "ngẫu nhiên, chỉ định tuyến"
    string secret_ref "tham chiếu secret store"
    string auth_mode "HMAC bắt buộc"
    string status "active, rotating, disabled"
  }
  PAYMENT_INTENT {
    uuid id PK
    string tenant_id
    uuid merchant_id FK "bất biến"
    uuid receiving_account_id FK "bất biến"
    bigint amount_vnd "bất biến"
    string currency "VND"
    json beneficiary_snapshot "bất biến"
    string payment_reference UK "unique trong project; tra theo receiver"
    int reference_profile_version FK "snapshot, không rewrite"
    string host_ref_type
    string host_ref_id
    string idempotency_key UK "tenant + key"
    string request_fingerprint
    timestamptz expires_at
    string status
  }
  WEBHOOK_INBOX {
    uuid id PK
    string tenant_id
    uuid connection_id FK
    string event_key UK "connection + id giao dịch provider"
    bytes raw_body "hạn lưu giữ, hạn chế truy cập"
    string status
    int attempts
    timestamptz next_attempt_at
    timestamptz lease_until
    string last_error_code
  }
  PROVIDER_TRANSACTION {
    uuid id PK
    string tenant_id
    uuid receiving_account_id FK
    string provider
    string provider_account_key "ổn định qua xoay credential"
    string webhook_tx_id "ID số trong webhook"
    string api_tx_id "UUID API v2, ánh xạ CHƯA xác minh"
    string dedup_key UK "provider + account_key + tx id"
    bigint amount_vnd
    string direction "in, out"
    string memo
    string reported_receiver_masked "receiver trong payload, để so khớp"
    timestamptz occurred_at
    string first_source "webhook, reconcile"
  }
  SETTLEMENT {
    uuid id PK
    string tenant_id
    uuid transaction_id FK,UK
    uuid intent_id FK,UK "một intent một settlement"
    bigint amount_vnd "= intent.amount_vnd"
    string origin "auto, operator_review"
    timestamptz settled_at
  }
  REVIEW_CASE {
    uuid id PK
    string tenant_id
    uuid transaction_id FK
    uuid candidate_intent_id "tuỳ chọn"
    string reason
    string status "open, resolved"
    string resolution
    string resolved_by
  }
  OUTBOX_EVENT {
    uuid event_id PK
    string tenant_id
    string type "PaymentSettled, PaymentNeedsReview"
    uuid aggregate_id
    json payload
    string status
    int attempts
  }
  RECONCILIATION_RUN {
    uuid id PK
    string tenant_id
    uuid connection_id FK
    timestamptz window_from
    timestamptz window_to
    string cursor
    string status
  }
```
