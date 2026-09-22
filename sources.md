# Nguồn, provenance và giới hạn

[← Mục lục](readme.md)

Bộ tài liệu xuất ngày 21/09/2026 từ thiết kế Payment-only đã cập nhật prefix. Tài liệu và sơ đồ là kiến trúc đề xuất; nghiên cứu nguồn chủ yếu ngày 17/09, riêng trang cấu hình mã được đọc ngày 21/09. Không khẳng định có kiểm chứng giao dịch live hoặc benchmark mới.

## Nguồn chính thức và tham khảo

- S1: [SePay xác thực webhook](https://developer.sepay.vn/vi/sepay-webhooks/xac-thuc).
- S2: [SePay tích hợp webhook](https://docs.sepay.vn/tich-hop-webhooks.html).
- S3: [SePay lỗi, retry, ordering](https://developer.sepay.vn/vi/sepay-webhooks/xu-ly-loi).
- S4: [SePay đối soát giao dịch](https://developer.sepay.vn/vi/sepay-webhooks/doi-soat-giao-dich).
- S7: [Redis Pub/Sub semantics](https://redis.io/docs/latest/develop/pubsub/).
- S8: [AWS transactional outbox](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html).
- S15: [PyPA plugin discovery](https://packaging.python.org/en/latest/guides/creating-and-discovering-plugins/).
- S16: [pytest-dev/pluggy](https://github.com/pytest-dev/pluggy).
- S17 trong bộ architecture: [SePay cấu hình mã thanh toán](https://developer.sepay.vn/vi/sepay-webhooks/cau-hinh-ma-thanh-toan). ID nguồn này thuộc bộ architecture, không dùng số thứ tự của báo cáo restaurant cũ.
- S18: [EMVCo QR Code Specification for Payment Systems — Merchant-Presented Mode](https://www.emvco.com/emv-technologies/qr-codes/): cấu trúc TLV (ID 2 số + độ dài 2 số + giá trị), tag 00/01/53/54/58/62/63 và CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF) tính trên chuỗi tới hết `6304`; dùng cho builder VietQR của adapter SePay.
- S19: NAPAS — đặc tả VietQR (merchant account information tag 38: GUID `A000000727`, beneficiary `00` BIN + `01` số tài khoản, service code `QRIBFTTA` chuyển tới tài khoản; tag 62 sub-tag 08 = nội dung chuyển khoản). Vector test tự dựng, kiểm chéo CRC với `binascii.crc_hqx` (thư viện chuẩn Python) ngày 22/09/2026.
- S20: [SePay API v2 giao dịch](https://developer.sepay.vn/vi/sepay-webhooks/doi-soat-giao-dich) đọc lại 22/09/2026: `GET https://userapi.sepay.vn/v2/transactions`, `Authorization: Bearer`, `transaction_date_from/to`, `bank_account_id`, `per_page` ≤ 100, `page`, `since_id` (UUID), mảng `data`, giới hạn 3 request/giây (429). Chưa xác minh trên SePay Test.
- [SePay Laravel package](https://github.com/sepayvn/laravel-sepay): ví dụ config/migration/event listener, không phải bằng chứng module Python sẵn có.
- [Unofficial Python SePay SDK](https://github.com/shinxz12/sepay): tham khảo API client, chưa audit làm lõi/payment engine.
- [AWS outbox sample](https://github.com/aws-samples/transactional-outbox-pattern).
- [FastAPI async/concurrency](https://fastapi.tiangolo.com/async/), [Node.js event loop](https://nodejs.org/en/learn/asynchronous-work/dont-block-the-event-loop): không dùng làm benchmark ứng dụng cụ thể.

Ưu tiên docs nhà cung cấp cho contract; source code cho hiện trạng; blog/forum cho góc nhìn chứ không cho bảo đảm tài chính. Google trả trang chuyển hướng trong lượt nghiên cứu trước, nên không có kết quả Google được dùng làm bằng chứng. Nội dung nghiên cứu restaurant/offline/CRDT không được đưa vào phạm vi package này.

## Nguồn dự án đã quan sát

MeowAI webhook router, payment models, entitlement service, orders repository và VietQR builder cung cấp bối cảnh extraction. Những đường dẫn xuất hiện trong bản chụp architecture-design là provenance của dự án nguồn, không phải file hiện có tại thư mục payment_modules_v1. Bộ tài liệu không cần truy cập các đường dẫn đó để hiểu target design.

Ma trận [evidence-matrix.md](evidence-matrix.md) bảo toàn yêu cầu và nguồn. Các nhãn xác nhận trong brief điều phối không đồng nghĩa mọi chi tiết table/API đã được user chốt riêng; xem sổ quyết định.
