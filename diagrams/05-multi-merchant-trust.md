# D05 — Ranh giới tin cậy multi-merchant

[← Mục lục](../readme.md) · [Danh mục sơ đồ](../diagrams.md)

Trạng thái: sơ đồ kiến trúc đã đối chiếu với các delta đã duyệt (22/09/2026); không phải bằng chứng triển khai. Mermaid đã sửa sau lần render 21/09/2026 và chưa render lại.

Locator trên URL chỉ để tìm connection. Merchant context chỉ được tin sau khi HMAC trên raw body hợp lệ với secret của chính connection đó.

- Không đọc tenant/merchant từ body hay header do bên gửi tự khai.
- Locator không tồn tại hoặc connection `disabled` → **404 đồng nhất** (D5), không lộ connection nào có thật. Chữ ký hoặc timestamp sai → 401. Connection `pending`/`not_ready` vẫn nhận và lưu webhook đã xác thực (tiền thật không bị bỏ), chỉ không nhận intent mới.
- Ingest chỉ đọc trường `id` sau HMAC; thiếu id → inbox `quarantined` với khoá `sha256(raw_body)`, vẫn ACK 200.
- Guard lõi chạy trước policy và xét **chiều tiền trước**: tiền ra/`unknown` → fact vẫn lưu, `not_applicable`, không review. Tiền vào có receiver không thuộc binding → fact lưu với `receiving_account_id NULL`, review `RECEIVER_UNBOUND`. Mã khớp intent của tenant khác → review `TENANT_MISMATCH` + alert + metric (D6). Không nhánh nào settle.
- Merchant của connection, receiving account và intent phải trùng nhau (cùng tenant chưa đủ); environment Test/Live cũng nằm trong composite FK.

```mermaid
---
config:
  theme: base
  layout: elk
  fontFamily: Arial, Helvetica, sans-serif
  flowchart:
    curve: linear
    nodeSpacing: 55
    rankSpacing: 70
    diagramPadding: 32
    wrappingWidth: 230
  themeVariables:
    fontFamily: "Arial, Helvetica, sans-serif"
    fontSize: 15px
---
flowchart TB
  IN["POST /payments/sepay/{locator}<br/>raw body + X-SePay-Signature + X-SePay-Timestamp"]
  subgraph UNTRUSTED["Vùng CHƯA tin cậy — chỉ là định tuyến"]
    direction TB
    L1["Tra ProviderConnection theo locator<br/>(locator KHÔNG phải xác thực)"]
    L2{"Connection tồn tại<br/>và không disabled?"}
    V1["Lấy secret đang hiệu lực<br/>(hiện tại + cũ trong cửa sổ xoay)"]
    V2{"Timestamp hợp lệ<br/>và HMAC khớp?"}
  end
  subgraph TRUSTED["Vùng tin cậy — merchant context đã xác thực"]
    direction TB
    T1["Tenant + merchant lấy từ connection,<br/>không lấy từ body/header"]
    T2["Ghi WebhookInbox bền vững<br/>unique(connection, event_key)"]
    T2Q["Thiếu id → quarantined<br/>khoá sha256(raw_body), ACK 200"]
    T3["Worker: normalize → ghi Observation<br/>+ ProviderTransaction canonical (fact)"]
    T4{"Guard lõi: chiều tiền?"}
    T4B{"Receiver thuộc binding<br/>của connection (cùng merchant)?"}
    T5["ReferenceResolver → IntentEligibility<br/>→ MatchingPolicy; intent lọc theo<br/>tenant + merchant + environment + account"]
    T5T["Mã khớp intent tenant khác<br/>→ review TENANT_MISMATCH<br/>+ alert + metric"]
    T6["Composite FK (tenant, merchant, environment, …)<br/>chặn liên kết chéo tenant/merchant/env"]
  end
  R401["401 — không lưu dữ liệu nghiệp vụ<br/>chỉ đếm metric từ chối"]
  R404["404 đồng nhất<br/>locator lạ hoặc disabled"]
  QOUT["Tiền ra / unknown → not_applicable<br/>fact vẫn lưu, không review"]
  Q["Receiver lệch → review RECEIVER_UNBOUND<br/>fact lưu, receiving_account_id NULL"]
  OK["Settle nếu đúng tiền và đủ điều kiện;<br/>ngược lại review"]
  IN --> L1 --> L2
  L2 -->|"không"| R404
  L2 -->|"có"| V1 --> V2
  V2 -->|"không"| R401
  V2 -->|"có"| T1 --> T2 --> T3 --> T4
  T2 -->|"không có id"| T2Q
  T4 -->|"ra / unknown"| QOUT
  T4 -->|"vào"| T4B
  T4B -->|"không"| Q
  T4B -->|"có"| T5 --> OK
  T5 -->|"tenant khác"| T5T
  T6 -.-> T5
  classDef un fill:#fff7ed,stroke:#c2410c,color:#431407,stroke-width:1.5px;
  classDef tr fill:#ecfdf5,stroke:#047857,color:#052e1c,stroke-width:1.5px;
  classDef bad fill:#ffe4e6,stroke:#be123c,color:#4c0519,stroke-width:1.5px;
  classDef good fill:#dcfce7,stroke:#15803d,color:#052e16,stroke-width:2px;
  class L1,L2,V1,V2 un;
  class T1,T2,T2Q,T3,T4,T4B,T5,T6 tr;
  class R401,R404,Q,QOUT,T5T bad;
  class OK good;
```
