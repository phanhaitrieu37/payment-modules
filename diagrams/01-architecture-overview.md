# D01 — Tổng quan kiến trúc

[← Mục lục](../readme.md) · [Danh mục sơ đồ](../diagrams.md)

Trạng thái: sơ đồ kiến trúc đề xuất; không phải bằng chứng triển khai. Nguồn Mermaid được chuyển nguyên từ bản thiết kế đã render/kiểm tra ngày 21/09/2026.

Mỗi dự án cài package riêng, có database riêng và nhận webhook trực tiếp từ SePay. Khách chuyển khoản thẳng vào tài khoản ngân hàng của từng merchant.

- Không có dịch vụ payment trung tâm dùng chung giữa các dự án.
- Package không thu hộ, không chia tiền, không payout — chỉ ghi nhận và đối chiếu tiền đã vào tài khoản merchant.
- Đối soát là package chủ động đọc API giao dịch; mũi tên nét đứt chỉ luồng dữ liệu đọc về.

```mermaid
---
config:
  theme: base
  fontFamily: Arial, Helvetica, sans-serif
  layout: elk
  flowchart:
    curve: linear
    nodeSpacing: 60
    rankSpacing: 80
    diagramPadding: 32
    wrappingWidth: 220
  themeVariables:
    fontFamily: "Arial, Helvetica, sans-serif"
    fontSize: 15px
---
flowchart TB
  subgraph EXT["Bên ngoài — không thuộc package"]
    direction LR
    CUS["Khách hàng<br/>(app ngân hàng)"]
    BANK["Ngân hàng của merchant<br/>tài khoản nhận tiền"]
    SEPAY["SePay<br/>webhook + API đối soát"]
  end
  subgraph HOSTA["Dự án A — tự vận hành (ví dụ: SaaS thuê bao)"]
    direction TB
    A_APP["Nghiệp vụ host A<br/>giá, đơn, thuế, cấp quyền"]
    A_PKG["Package payment<br/>(cài riêng, version pin)"]
    A_DB[("PostgreSQL của A<br/>bảng payment do host migrate")]
    A_APP -->|"tạo intent / nhận settlement"| A_PKG
    A_PKG -->|"UoW + ràng buộc"| A_DB
  end
  subgraph HOSTB["Dự án B — tự vận hành (ví dụ: F&B)"]
    direction TB
    B_APP["Nghiệp vụ host B<br/>bill, thuế, hoàn tất đơn"]
    B_PKG["Package payment<br/>(cài riêng, version pin)"]
    B_DB[("PostgreSQL của B")]
    B_APP -->|"tạo intent / nhận settlement"| B_PKG
    B_PKG -->|"UoW + ràng buộc"| B_DB
  end
  CUS -->|"chuyển khoản trực tiếp<br/>theo chỉ dẫn + mã tham chiếu"| BANK
  BANK -->|"biến động số dư"| SEPAY
  SEPAY -->|"webhook ký HMAC<br/>(endpoint theo từng connection)"| A_PKG
  SEPAY -->|"webhook ký HMAC"| B_PKG
  SEPAY -.->|"trả dữ liệu giao dịch<br/>(package gọi API đối soát)"| A_PKG
  SEPAY -.->|"trả dữ liệu giao dịch<br/>(package gọi API)"| B_PKG
  NOTE["Không có dịch vụ trung tâm dùng chung.<br/>Không thu hộ, không chia tiền, không payout.<br/>Mỗi dự án có nhiều merchant, mỗi merchant nhận tiền vào tài khoản riêng."]
  classDef ext fill:#f1f5f9,stroke:#64748b,color:#0f172a,stroke-width:1.5px;
  classDef pkg fill:#dbeafe,stroke:#1d4ed8,color:#0b1b3f,stroke-width:2px;
  classDef host fill:#fef3c7,stroke:#b45309,color:#3b2303,stroke-width:1.5px;
  classDef db fill:#ecfdf5,stroke:#047857,color:#052e1c,stroke-width:1.5px;
  classDef note fill:#faf5ff,stroke:#7c3aed,color:#2e1065,stroke-dasharray:6 4;
  class BANK,SEPAY,CUS ext;
  class A_PKG,B_PKG pkg;
  class A_APP,B_APP host;
  class A_DB,B_DB db;
  class NOTE note;
```
