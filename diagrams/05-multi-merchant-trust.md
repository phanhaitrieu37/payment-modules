# D05 — Ranh giới tin cậy multi-merchant

[← Mục lục](../readme.md) · [Danh mục sơ đồ](../diagrams.md)

Trạng thái: sơ đồ kiến trúc đề xuất; không phải bằng chứng triển khai. Nguồn Mermaid được chuyển nguyên từ bản thiết kế đã render/kiểm tra ngày 21/09/2026.

Locator trên URL chỉ để tìm connection. Merchant context chỉ được tin sau khi HMAC trên raw body hợp lệ với secret của chính connection đó.

- Không đọc tenant/merchant từ body hay header do bên gửi tự khai.
- Phản hồi đồng nhất khi connection không tồn tại, để không lộ connection nào có thật.
- Guard lõi chạy trước policy: receiver lệch → fact vẫn lưu, mở ReviewCase; tiền ra → ignored. Cả hai đều không settle.

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
    L2{"Connection<br/>active?"}
    V1["Lấy secret đang hiệu lực<br/>(hiện tại + cũ trong cửa sổ xoay)"]
    V2{"Timestamp hợp lệ<br/>và HMAC khớp?"}
  end
  subgraph TRUSTED["Vùng tin cậy — merchant context đã xác thực"]
    direction TB
    T1["Tenant + merchant lấy từ connection,<br/>không lấy từ body/header"]
    T2["Ghi WebhookInbox bền vững<br/>unique(connection, event_key)"]
    T3["Worker: parse + normalize<br/>ghi ProviderTransaction (fact)"]
    T4{"Guard lõi: receiver<br/>+ chiều tiền vào?"}
    T5["Mới tới MatchingPolicy;<br/>truy vấn intent lọc theo<br/>tenant_id + receiving_account_id"]
    T6["Composite FK (tenant_id, …)<br/>chặn liên kết chéo tenant"]
  end
  R401["401 — không lưu dữ liệu nghiệp vụ<br/>chỉ đếm metric từ chối"]
  R404["404/401 đồng nhất<br/>không lộ connection nào tồn tại"]
  Q["Receiver lệch → ReviewCase<br/>Tiền ra → ignored<br/>fact vẫn lưu, không settle"]
  OK["Tiếp tục ghi fact + matching"]
  IN --> L1 --> L2
  L2 -->|"không"| R404
  L2 -->|"có"| V1 --> V2
  V2 -->|"không"| R401
  V2 -->|"có"| T1 --> T2 --> T3 --> T4
  T4 -->|"không khớp / thiếu"| Q
  T4 -->|"khớp"| T5 --> OK
  T6 -.-> T5
  classDef un fill:#fff7ed,stroke:#c2410c,color:#431407,stroke-width:1.5px;
  classDef tr fill:#ecfdf5,stroke:#047857,color:#052e1c,stroke-width:1.5px;
  classDef bad fill:#ffe4e6,stroke:#be123c,color:#4c0519,stroke-width:1.5px;
  classDef good fill:#dcfce7,stroke:#15803d,color:#052e16,stroke-width:2px;
  class L1,L2,V1,V2 un;
  class T1,T2,T3,T4,T5,T6 tr;
  class R401,R404,Q bad;
  class OK good;
```
