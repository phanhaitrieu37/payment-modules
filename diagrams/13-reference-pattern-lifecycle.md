# D13 — Vòng đời mẫu mã thanh toán theo project

[← Mục lục](../readme.md) · [Danh mục sơ đồ](../diagrams.md)

Trạng thái: sơ đồ kiến trúc đề xuất; không phải bằng chứng triển khai. Nguồn Mermaid được chuyển nguyên từ bản thiết kế đã render/kiểm tra ngày 21/09/2026.

Tiền tố đặt theo project. Profile bất biến có version → áp vào mẫu mã cấp công ty SePay của từng merchant (thủ công, có checklist) → sinh mã trong QR → SePay trích code → bộ lọc từng webhook → server xác thực và khớp nguyên reference. Xoay profile giữ nguyên v1 cho tiền muộn.

- Khối màu xanh dương là hành vi SePay đã đọc trong tài liệu (S17); khối màu tím là đề xuất của tài liệu này; khối vàng là điểm quyết định.
- Tiền tố không phải xác thực: HMAC, receiver, tenant và dedup vẫn chạy như cũ.
- Biên của regex SePay (hậu tố dài hơn max, mã nằm lọt trong chuỗi dài) chưa được tài liệu mô tả; phải thử ở Test mode và với nội dung thật của ngân hàng trước production.
- Không có API tự tạo mẫu hay đăng ký mã theo từng intent; package không hứa đồng bộ tự động.

```mermaid
---
config:
  theme: base
  fontFamily: Arial, Helvetica, sans-serif
  layout: elk
  flowchart:
    curve: linear
    nodeSpacing: 50
    rankSpacing: 70
    diagramPadding: 32
    wrappingWidth: 240
  themeVariables:
    fontFamily: "Arial, Helvetica, sans-serif"
    fontSize: 15px
---
flowchart TB
  subgraph CFG["1. Project (package, DB cục bộ) — đề xuất"]
    direction TB
    PR["ReferenceProfile v1 (bất biến)<br/>tiền tố project, vd ABC<br/>hậu tố độ dài cố định, bảng ký tự A–Z0–9<br/>ví dụ 12 ký tự — chưa chốt"]
    GEN["PaymentReferenceGenerator<br/>hậu tố ngẫu nhiên, unique DB, retry khi trùng<br/>không phải token bảo mật"]
    INT["PaymentIntent<br/>snapshot reference + profile_version"]
  end
  subgraph MER["2. Mỗi merchant connection — onboarding thủ công"]
    direction TB
    TPL["Mẫu mã ở cấp CÔNG TY SePay của merchant<br/>tiền tố 2–5, hậu tố min/max 1–30<br/>kiểu số hoặc số+chữ; Test/Live riêng — S17"]
    CHK["Checklist + trạng thái ready<br/>mẫu khớp profile, binding tài khoản,<br/>bộ lọc webhook; bằng chứng xác minh thủ công"]
  end
  subgraph RUN["3. Mỗi giao dịch"]
    direction TB
    QR["QR / nội dung CK<br/>chứa ABC + hậu tố"]
    EXT["SePay trích code<br/>mẫu bật theo thứ tự, khớp đầu tiên thắng<br/>tắt nhận diện → code rỗng"]
    FIL("Bộ lọc từng webhook<br/>chỉ khi có code, theo tiền tố")
    DROP["Không gửi webhook<br/>chỉ đối soát API tìm lại"]
    VER["HMAC, receiver, tenant, dedup<br/>(tiền tố không bỏ qua được)"]
    MAT("Khớp NGUYÊN reference<br/>trong project, scope receiver")
    OK["Settlement nếu đúng tiền"]
    REV["ReviewCase<br/>không có / mơ hồ / mâu thuẫn"]
  end
  subgraph ROT["4. Xoay profile"]
    direction TB
    V2["Profile v2: tiền tố/độ dài mới"]
    GATE("Mọi connection liên quan<br/>ready với v2? Chưa → v1 vẫn active")
    CUT["Cutover: v2 active sinh mã mới"]
    KEEP["Giữ mẫu + bộ lọc v1<br/>cho intent cũ và tiền muộn;<br/>không gỡ chỉ vì hết hạn"]
  end
  PR --> GEN --> INT --> QR --> EXT --> FIL
  PR -.->|"áp dụng"| TPL --> CHK
  CHK -.->|"ready"| EXT
  FIL -->|"qua lọc"| VER --> MAT
  FIL -->|"bị lọc"| DROP
  MAT -->|"một ứng viên"| OK
  MAT -->|"khác"| REV
  INT -.->|"version sau"| V2
  V2 --> GATE
  GATE -->|"có"| CUT
  CUT --> KEEP
  classDef prop fill:#ede9fe,stroke:#6d28d9,color:#1e1b4b,stroke-width:1.5px;
  classDef ext fill:#dbeafe,stroke:#1d4ed8,color:#0b1b3f,stroke-width:1.5px;
  classDef ok fill:#dcfce7,stroke:#15803d,color:#052e16,stroke-width:2px;
  classDef bad fill:#ffe4e6,stroke:#be123c,color:#4c0519,stroke-width:1.5px;
  classDef gate fill:#fef3c7,stroke:#b45309,color:#3b2303,stroke-width:1.5px;
  class PR,GEN,INT,CHK,V2,CUT,KEEP,VER prop;
  class TPL,QR,EXT,DROP ext;
  class OK ok;
  class REV bad;
  class FIL,MAT,GATE gate;
```
