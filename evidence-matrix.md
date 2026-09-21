# Ma trận bằng chứng và độ bao phủ

[← Mục lục](readme.md)

Bản chuyển từ ma trận kiến trúc nguồn. `confirmed` ở nguồn có thể là ràng buộc trong brief điều phối, không đồng nghĩa từng chi tiết đã được người dùng quyết định riêng. [Sổ quyết định](decisions-and-open-questions.md) phân biệt quyết định trực tiếp của người dùng với phương án thiết kế.

| ID | Nội dung | Nhãn nguồn | Nguồn | Sơ đồ / cách biểu diễn |
|---|---|---|---|---|
| R01 | Chỉ Payment; package cài riêng vào từng dự án, tự vận hành, không có service trung tâm | confirmed | Brief 21/09/2026 | D01; direct |
| R02 | Provider duy nhất ở bản đầu: SePay | confirmed | Brief 21/09/2026 | D01 D02 D03; direct |
| R03 | Mở rộng OOP bằng interface nhỏ + composition/DI, không kế thừa sâu | confirmed | Brief 21/09/2026 | D02 D03; direct |
| R04 | Mỗi dự án nhiều merchant ngay từ đầu; khách trả thẳng vào tài khoản merchant; không thu hộ/chia tiền/payout | confirmed | Brief 21/09/2026 | D01 D04 D05; direct |
| R05 | Phân lớp Domain / Application / Ports / Adapters | confirmed | Brief 21/09/2026 | D02; direct |
| R06 | Host sở hữu giá, đơn, thuế, fulfillment | confirmed | Brief 21/09/2026 | D02 D06 D12; direct |
| R07 | SePay HMAC-SHA256 trên {timestamp}.{raw_body}, header X-SePay-Signature / X-SePay-Timestamp, ví dụ lệch 300 giây | verified_external | S1 — https://developer.sepay.vn/vi/sepay-webhooks/xac-thuc | D05 D06 D07 D11; direct |
| R08 | ACK thành công HTTP 200/201 {"success": true} trong 30 giây; webhook có thể trùng; retry hữu hạn (1 + 7 lần ~33 phút, ngưỡng quét 5 giờ) | verified_external | S2 S3 — https://docs.sepay.vn/tich-hop-webhooks.html https://developer.sepay.vn/vi/sepay-webhooks/xu-ly-loi | D06 D07; direct |
| R09 | Không bảo đảm thứ tự webhook; có hướng dẫn đối soát qua API | verified_external | S3 S4 — https://developer.sepay.vn/vi/sepay-webhooks/xu-ly-loi https://developer.sepay.vn/vi/sepay-webhooks/doi-soat-giao-dich | D08; direct |
| R10 | Ánh xạ ID số trong webhook ↔ UUID trong API v2 CHƯA xác minh; không giả định bằng nhau | unresolved | S3 S4 — https://developer.sepay.vn/vi/sepay-webhooks/xu-ly-loi https://developer.sepay.vn/vi/sepay-webhooks/doi-soat-giao-dich | D04 D08; direct |
| R11 | Locator của endpoint chỉ định tuyến, không phải xác thực; verify trước khi tin merchant context; kiểm tra tài khoản nhận | confirmed | Brief 21/09/2026 | D05 D06 D11; direct |
| R12 | Định danh tài khoản provider ổn định qua xoay credential / tạo lại connection, dùng cho dedup | confirmed | Brief 21/09/2026 | D04; direct |
| R13 | Tên trường tài khoản nhận trong payload webhook và định danh tài khoản phía SePay dùng làm provider_account_key | unresolved | S2 — https://docs.sepay.vn/tich-hop-webhooks.html | D04 D05; direct |
| R14 | PaymentIntent bất biến về số tiền + snapshot người thụ hưởng | confirmed | Brief 21/09/2026 | D04 D09; direct |
| R15 | Mặc định chỉ khớp đúng số tiền; lệch tiền / trả muộn → review | confirmed | Brief 21/09/2026 | D03 D07 D09 D11; direct |
| R16 | Partial payment, refund, chia bill: chưa xác nhận — là quyết định mở, không phải tính năng hứa | unresolved | Brief 21/09/2026 | D09; direct |
| R17 | Inbox bền vững trước khi ACK | confirmed | Brief 21/09/2026 | D06 D07 D10 D11; direct |
| R18 | Retry an toàn nhờ ràng buộc unique trong DB | confirmed | Brief 21/09/2026 | D04 D07 D11; direct |
| R19 | Settlement và outbox commit nguyên tử | confirmed | Brief 21/09/2026 | D06 D11 D12; direct |
| R20 | Handler cùng UoW cùng database giữ nguyên tử payment + quyền lợi; phương án async cần fulfillment idempotent + theo dõi retry | confirmed | Brief 21/09/2026 | D12; direct |
| R21 | Không tuyên bố exactly-once qua mạng; at-least-once + idempotent | confirmed | Brief 21/09/2026 | D12; direct |
| R22 | Fact ngân hàng lưu độc lập với kết quả matching | confirmed | Brief 21/09/2026 | D04 D07 D10 D11; direct |
| R23 | Không bao giờ đánh dấu đã trả từ browser return hoặc khi tạo QR | confirmed | Brief 21/09/2026 | D06 D09; direct |
| R24 | Adapter SQLAlchemy/PostgreSQL; migration do host chạy; FastAPI router tuỳ chọn | confirmed | Brief 21/09/2026 | D02; direct |
| R25 | Mọi truy vấn và ràng buộc theo tenant; composite FK chặn liên kết chéo tenant | confirmed | Brief 21/09/2026 | D04 D05; direct |
| R26 | Triển khai hiện có ở một host: verify HMAC raw body + timestamp, dedup unique theo ID giao dịch, khớp đúng tiền hoặc vào hàng unmatched, ghi tiền + trạng thái đơn + quyền lợi trong một transaction | observed_implementation | Code host hiện có | ; aggregated — Trình bày ở mục văn bản Hiện trạng quan sát; không vẽ vì không phải kiến trúc đề xuất |
| R27 | Khoảng trống của triển khai hiện có so với đề xuất: một secret toàn cục, một merchant, dedup chưa theo tài khoản, xử lý inline chưa có inbox | observed_implementation | Code host hiện có | ; aggregated — Trình bày ở mục văn bản Hiện trạng quan sát; không vẽ vì không phải kiến trúc đề xuất |
| R28 | Redis Pub/Sub at-most-once; transactional outbox cho lỗi ghi DB xong nhưng phát sự kiện hỏng; bên nhận vẫn phải chống trùng | verified_external | S7 S8 — https://redis.io/docs/latest/develop/pubsub/ https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html | D12; direct |
| R29 | Đăng ký plugin tường minh lúc khởi động; entry points/pluggy chỉ khi thật sự có nhiều plugin | verified_external | S15 S16 — https://packaging.python.org/en/latest/guides/creating-and-discovering-plugins/ https://github.com/pytest-dev/pluggy | D02; direct |
| R30 | create_intent idempotent: tenant + key + fingerprint; cùng key khác payload → conflict | proposed | Đề xuất thiết kế | D04 D06; direct |
| R31 | Cấu hình mã thanh toán SePay ở cấp công ty: bật/tắt nhận diện toàn cục (tắt → code rỗng); tiền tố 2–5 ký tự, lưu thành chữ hoa, so khớp không phân biệt hoa/thường; hậu tố min/max 1–30 (max ≥ min, mặc định 6/8), kiểu số 0–9 hoặc số+chữ A–Z0–9; nhiều mẫu duyệt theo thứ tự khai báo, mẫu khớp đầu tiên thắng; mẫu đầu tiên không xoá được nhưng tắt được; Test và Live độc lập | verified_external | S17 — https://developer.sepay.vn/vi/sepay-webhooks/cau-hinh-ma-thanh-toan | D13; direct |
| R40 | Webhook SePay có bộ lọc riêng: chỉ gửi khi có mã thanh toán (bỏ giao dịch không khớp mẫu) và lọc theo tiền tố của code đã trích | verified_external | S17 — https://developer.sepay.vn/vi/sepay-webhooks/cau-hinh-ma-thanh-toan | D06 D11 D13; direct |
| R41 | Tiền tố mã thanh toán đặt theo project (không theo merchant) | confirmed | Quyết định người dùng 21/09/2026 | D04 D13; direct |
| R42 | ReferenceProfile bất biến, có version: tiền tố, hậu tố độ dài cố định, bảng ký tự (đề xuất chỉ ASCII A–Z/0–9); hậu tố ngẫu nhiên + unique DB + retry khi trùng, không phải token bảo mật; 12 ký tự chỉ là ví dụ mặc định đề xuất, không phải giá trị đã chốt hay bảo đảm của provider | proposed | Đề xuất thiết kế | D02 D03 D04 D13; direct |
| R43 | Intent snapshot payment_reference + profile_version; reference unique trong project, tra cứu có scope theo receiver; không rewrite snapshot cũ | proposed | Đề xuất thiết kế | D04 D06 D13; direct |
| R44 | Checklist onboarding cho từng merchant connection: áp profile của project vào mẫu mã ở cấp CÔNG TY SePay của merchant, binding tài khoản, bộ lọc từng webhook; bằng chứng xác minh thủ công, không phải chứng thực từ xa; không có control plane tự động | proposed | Đề xuất thiết kế | D02 D03 D04 D13; direct |
| R45 | Xoay profile: chỉ cutover generation mới khi các connection liên quan đã ready; giữ mẫu nhận diện VÀ bộ lọc webhook của version cũ cho intent cũ/tiền muộn, không gỡ chỉ vì intent hết hạn; một version active mỗi project, các version lịch sử vẫn được chấp nhận | proposed | Đề xuất thiết kế | D04 D13; direct |
| R46 | Server khớp NGUYÊN mã tham chiếu; nhiều ứng viên mâu thuẫn hoặc mơ hồ → review, không đoán; parser nội dung dự phòng chỉ chạy sau xác thực + kiểm tra receiver và không cứu được webhook đã bị SePay lọc bỏ (việc đó thuộc đối soát API) | proposed | Đề xuất thiết kế | D03 D06 D11 D13; direct |
| R47 | Biên của regex SePay (hậu tố dài hơn max, mã nằm lọt trong chuỗi dài hơn, ký tự liền kề) chưa được tài liệu mô tả: phải thử ở Test mode và với mẫu nội dung thật của ngân hàng trước production | unresolved | S17 — https://developer.sepay.vn/vi/sepay-webhooks/cau-hinh-ma-thanh-toan | D13; direct |
| R48 | Nhiều project độc lập dùng chung một tài khoản nhận: chọn tiền tố rời nhau + ghi rõ ownership khi onboarding; DB cục bộ không bảo đảm unique giữa các bản cài độc lập | proposed | Đề xuất thiết kế | D13; direct |
| R49 | API tự tạo/đồng bộ mẫu mã thanh toán hay đăng ký mã theo từng intent: không có bằng chứng; không hứa auto-sync (phương án autoprovision bị loại) | unresolved | S17 — https://developer.sepay.vn/vi/sepay-webhooks/cau-hinh-ma-thanh-toan | D13; direct |
| R32 | Xoay secret: cho phép secret cũ + mới trong cửa sổ xoay | proposed | Đề xuất thiết kế | D03 D05; direct |
| R33 | Worker claim bằng lease + SKIP LOCKED; lease hết hạn thì claim lại | proposed | Đề xuất thiết kế | D06 D07 D10; direct |
| R34 | Đối soát không chứng minh được identity → ReviewCase, không tự settle | proposed | Đề xuất thiết kế | D08; direct |
| R37 | Tách ba lớp: giao dịch ngân hàng đã xác thực / intent đã khớp / hành động nghiệp vụ đã hoàn tất; fulfillment lỗi không đảo trạng thái paid | confirmed | Brief 21/09/2026 | D07 D10 D12; direct |
| R38 | Guard lõi (receiver thuộc connection, chiều tiền vào, tenant) chạy TRƯỚC MatchingPolicy; policy tuỳ biến không thể cho phép receiver lạ hay tiền ra | confirmed | Brief 21/09/2026 | D03 D05 D11; direct |
| R39 | Giao dịch unmatched được lưu bền và rematch được khi intent tới muộn hoặc operator gán | confirmed | Brief 21/09/2026 | D07 D10; direct |
| R35 | Restaurant LAN / edge / kitchen / stack | out_of_scope | Brief 21/09/2026 | ; omitted — Brief loại trừ rõ; chỉ giữ ví dụ tích hợp ngắn |
| R36 | Hoá đơn điện tử / VAT, subscription, credit trong lõi payment | out_of_scope | Nghiên cứu 17/09/2026 | ; omitted — Thuộc host hoặc module riêng nhận sự kiện thanh toán |

Coverage ghi nhận cách thể hiện yêu cầu, không chứng minh tính năng đã được code hoặc test.
