# D12 — Hai phương án tích hợp host

[← Mục lục](../readme.md) · [Danh mục sơ đồ](../diagrams.md)

Trạng thái: sơ đồ kiến trúc đề xuất; không phải bằng chứng triển khai. Nguồn Mermaid được chuyển nguyên từ bản thiết kế đã render/kiểm tra ngày 21/09/2026.

Phương án A giữ nguyên tử tiền + quyền lợi khi host dùng chung database. Phương án B tách bất đồng bộ qua outbox, đổi lại cần theo dõi fulfillment và retry idempotent.

- Không có cam kết exactly-once qua mạng; đảm bảo là at-least-once kèm idempotency ở bên nhận.
- Ví dụ SaaS và F&B chỉ minh hoạ cách tích hợp, không phải thiết kế hệ thống của các dự án đó.

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
flowchart LR
  SET["Settlement quyết định<br/>(trong package)"]
  subgraph OPT_A["Phương án A — handler đồng bộ cùng UoW (cùng database)"]
    direction TB
    A1["SettlementHandler.on_settled(uow, settlement)"]
    A2["Ví dụ SaaS: cấp quyền gói<br/>trong cùng transaction"]
    A3["Commit một lần:<br/>tiền + trạng thái + quyền lợi"]
    A1 --> A2 --> A3
  end
  subgraph OPT_B["Phương án B — handler bất đồng bộ qua outbox"]
    direction TB
    B1["OutboxEvent PaymentSettled<br/>commit cùng Settlement"]
    B2["Dispatcher gửi lại tới khi<br/>consumer xác nhận"]
    B3["Consumer lưu receipt(event_id) unique<br/>+ thay đổi nghiệp vụ cùng transaction"]
    B4["Theo dõi fulfillment:<br/>pending / failed / done + retry"]
    B5["Ví dụ F&B: đánh dấu bill đã trả"]
    B1 --> B2 --> B3 --> B5
    B3 --> B4
  end
  SET --> A1
  SET --> B1
  RULE["Host sở hữu giá, đơn, thuế, hoá đơn, fulfillment.<br/>Không callback tuỳ ý commit riêng hay gọi API ngoài trong transaction thanh toán.<br/>Không cam kết exactly-once qua mạng: at-least-once + idempotent."]
  classDef a fill:#dcfce7,stroke:#15803d,color:#052e16,stroke-width:1.5px;
  classDef b fill:#dbeafe,stroke:#1d4ed8,color:#0b1b3f,stroke-width:1.5px;
  classDef note fill:#faf5ff,stroke:#7c3aed,color:#2e1065,stroke-dasharray:6 4;
  classDef core fill:#fef3c7,stroke:#b45309,color:#3b2303,stroke-width:2px;
  class A1,A2,A3 a;
  class B1,B2,B3,B4,B5 b;
  class RULE note;
  class SET core;
```
