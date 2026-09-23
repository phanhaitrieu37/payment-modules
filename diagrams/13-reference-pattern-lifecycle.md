# D13 — Vòng đời mẫu mã thanh toán theo project

[← Mục lục](../readme.md) · [Danh mục sơ đồ](../diagrams.md)

Trạng thái: sơ đồ kiến trúc đã đối chiếu với các delta đã duyệt (22/09/2026); không phải bằng chứng triển khai. Mermaid đã sửa sau lần render 21/09/2026 và chưa render lại.

Prefix cấu hình ở **cấp project** (không bao giờ theo merchant); mỗi version `ReferenceProfile` chứa **nhiều prefix có tên** (D1, vd `subscription` → `SUB`, `topup` → `TOP`) dùng chung `suffix_length` và alphabet. Profile bất biến có version → áp vào mẫu mã cấp công ty SePay của từng merchant (thủ công, có checklist theo từng prefix) → `CreateIntent(prefix_name)` sinh mã trong QR → SePay trích code → bộ lọc từng webhook theo prefix → server xác thực và khớp nguyên reference. Xoay profile giữ nguyên các version cũ còn accepted cho tiền muộn.

- Trong một version: tên prefix không trùng; giá trị prefix không trùng và không lồng nhau (`SUB`/`SUBX`) → lỗi `PrefixOverlap`. Giữa các version còn accepted: **không** từ chối; `check_prefix_overlap` chỉ trả advisory, vì `payment_reference` unique toàn project và khớp nguyên token nên một chuỗi chỉ thuộc một intent/một version.
- Readiness theo **connection × profile version × environment**. Checklist gồm một mục mẫu nhận diện cấp company của SePay và một mục bộ lọc webhook **cho mỗi prefix có tên**; mẫu của một prefix dùng suffix **min = min(L_i), max = max(L_i)** suy ra từ mọi version còn accepted dùng cùng prefix. Readiness bị vô hiệu (về `not_ready`) khi binding, environment, credential, tài khoản hoặc prefix/bộ lọc đổi sau khi đã ready.
- Kích hoạt v2 là **một transaction**: v2 → `active`, v1 → `accepted_legacy`; luôn đúng một active (partial unique). Chỉ kích hoạt khi mọi connection `active` đã ready với v2 và với prefix của mọi version cũ còn accepted.

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
    PR["ReferenceProfile v1 (bất biến, cấp project)<br/>prefix có tên: subscription=SUB, topup=TOP<br/>suffix_length + alphabet chung<br/>ví dụ 24 ký tự — chưa chốt"]
    GEN["PaymentReferenceGenerator(profile, prefix_name)<br/>tên lạ → UnknownReferencePrefix<br/>hậu tố ngẫu nhiên, unique DB, retry khi trùng"]
    INT["PaymentIntent<br/>snapshot reference + profile_version<br/>+ reference_prefix_name"]
  end
  subgraph MER["2. Mỗi merchant connection — onboarding thủ công"]
    direction TB
    TPL["Mẫu mã ở cấp CÔNG TY SePay của merchant<br/>tiền tố 2–5, hậu tố min/max 1–30<br/>kiểu số hoặc số+chữ; Test/Live riêng — S17"]
    CHK["Readiness connection × version × environment<br/>mỗi prefix có tên: 1 mẫu + 1 bộ lọc webhook<br/>suffix min/max phủ mọi version accepted<br/>bằng chứng xác minh thủ công"]
  end
  subgraph RUN["3. Mỗi giao dịch"]
    direction TB
    QR["QR / nội dung CK<br/>chứa SUB hoặc TOP + hậu tố"]
    EXT["SePay trích code<br/>mẫu bật theo thứ tự, khớp đầu tiên thắng<br/>tắt nhận diện → code rỗng"]
    FIL("Bộ lọc từng webhook<br/>chỉ khi có code, theo từng prefix có tên")
    DROP["Không gửi webhook<br/>chỉ đối soát API tìm lại"]
    VER["HMAC, receiver, tenant, dedup<br/>(tiền tố không bỏ qua được)"]
    MAT("Khớp NGUYÊN reference toàn project<br/>rồi kiểm scope tenant / env / receiver")
    OK["Settlement nếu đúng tiền"]
    REV["ReviewCase<br/>không có / mơ hồ / mâu thuẫn"]
  end
  subgraph ROT["4. Xoay profile"]
    direction TB
    V2["Profile v2: có thể GIỮ prefix, đổi suffix_length<br/>vd SUB/TOP, suffix 24 → 20<br/>trùng/lồng trong v2 → lỗi, với v1 → advisory"]
    GATE("Mọi connection active ready với v2<br/>và prefix của version cũ còn accepted?<br/>Chưa → v1 vẫn active")
    CUT["Cutover trong một transaction:<br/>v2 active, v1 accepted_legacy"]
    KEEP["Giữ mẫu + bộ lọc v1<br/>mẫu SUB min 20 max 24 phủ cả hai<br/>không gỡ chỉ vì intent hết hạn"]
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
