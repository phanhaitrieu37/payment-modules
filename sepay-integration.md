# Tích hợp SePay

[← Mục lục](readme.md) · [Nguồn chính thức](sources.md)

## Inbound webhook

Tài liệu đã đọc mô tả HMAC-SHA256 trên `{timestamp}.{raw_body}` cùng `X-SePay-Signature` và `X-SePay-Timestamp`; ví dụ chống replay dùng cửa sổ 300 giây. Thiết kế chọn HMAC, dù SePay có các chế độ xác thực khác. Verify bytes gốc trước parse; không serialize lại JSON để tính chữ ký.

Locator trên URL chỉ resolve connection. Sau verify, kiểm tra tài khoản thụ hưởng thuộc binding và trusted tenant/merchant. Payload có code/content, amount, direction và external ID; adapter chuẩn hoá nhưng không tự quyết định quyền lợi của host.

ACK đúng contract SePay: HTTP 200/201 và JSON `success: true` sau khi dữ liệu tiếp nhận đã được lưu bền. Lỗi database trước durable commit không trả thành công. Duplicate delivery đã ghi bền trả thành công mà không settle thêm lần nữa. Payload đã xác thực nhưng thiếu `id` được lưu inbox `quarantined` với khoá `sha256(raw_body)` và vẫn ACK 200 (retry của provider không sửa được body; raw body còn cho audit). Locator lạ hoặc connection `disabled` trả 404 đồng nhất; chữ ký/timestamp sai trả 401. Cửa sổ timestamp mặc định 300 giây (`timestamp_tolerance_seconds`, 60–7200). [SePay Test 22/09/2026](plans/reports/sepay-test-verification-260922.md) xác nhận retry mang `X-SePay-Timestamp` làm mới, nhưng header chậm khoảng 152 giây so với lúc nhận ở mọi lần gửi, nên không hạ tolerance dưới mức này; 300 giây còn dư khoảng 146 giây.

Retry provider hữu hạn: tài liệu đã đọc nêu một lần đầu và bảy retry theo Fibonacci, tổng lịch khoảng 33 phút; ngưỡng quét dự phòng 5 giờ không phải cam kết gửi liên tục 5 giờ. Không bảo đảm ordering.

## Mã và webhook filters

Recognition template là cấp công ty SePay, filter là cấp từng webhook. Mẫu gồm prefix, suffix min/max, character type và trạng thái; mỗi prefix có tên của profile cần một mẫu và một mục bộ lọc webhook; xem [payment-reference.md](payment-reference.md). Không đăng ký từng intent/code với provider theo cơ chế tài liệu này. Provider trích code từ giao dịch ngân hàng.

Khi “chỉ gửi khi có mã” bật, giao dịch không trích code sẽ không tới endpoint. Parser nội dung dự phòng chỉ áp dụng nếu payload thực sự đã được gửi. Giữ reconciliation để bù khoảng trống này.

## API đối soát

Adapter TransactionReader đọc theo cursor `since_id` và phân trang; cửa sổ ngày có chồng lấn chỉ để chạy lại. Tài liệu hiện đã đọc nêu API v2 giao dịch dùng UUID, pagination tối đa 100 và giới hạn 3 request/giây; xác minh lại contract/gói dịch vụ khi implement.

**Credential API là cấp company**, khác secret HMAC của một webhook: connection giữ `api_credential_ref` riêng, và reader lọc `bank_account_id = RECEIVING_ACCOUNT.provider_account_ref` thay vì quét cả company. **Rate limit tính theo IP** (tài liệu v2), nghĩa là mọi merchant của một bản cài chia chung quota: dùng một limiter chung cả process; công bằng giữa merchant do scheduler round-robin theo connection, không phải limiter theo connection.

Không coi numeric webhook ID bằng UUID API; cầu nối đề xuất là `referenceCode` (webhook) = `reference_number` (API). [SePay Test 22/09/2026](plans/reports/sepay-test-verification-260922.md) thấy hai giá trị bằng nhau và không rỗng ở 8/8 cặp, chỉ trên ACB — chưa đủ 20 cặp mỗi gateway nên `auto_settle` vẫn bị khoá. Mặc định `reconcile_mode = detect_only`: không chứng minh được identity thì mở review, không tự tạo thêm settlement. Ghi checkpoints sau xử lý bền vững, không trước.

## Merchant onboarding

Host xác nhận quyền sử dụng tài khoản và binding trước khi enable. Thiết lập endpoint, HMAC secret, tài khoản/VA và mẫu mã; Test/Live độc lập. Lưu bằng chứng cấu hình đã kiểm tra, không gắn nhãn live verified chỉ vì config local hợp lệ. Không có bằng chứng API tự đồng bộ mẫu ở tài liệu đã đọc; quy trình hiện đề xuất là thủ công.
