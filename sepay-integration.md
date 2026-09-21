# Tích hợp SePay

[← Mục lục](readme.md) · [Nguồn chính thức](sources.md)

## Inbound webhook

Tài liệu đã đọc mô tả HMAC-SHA256 trên `{timestamp}.{raw_body}` cùng `X-SePay-Signature` và `X-SePay-Timestamp`; ví dụ chống replay dùng cửa sổ 300 giây. Thiết kế chọn HMAC, dù SePay có các chế độ xác thực khác. Verify bytes gốc trước parse; không serialize lại JSON để tính chữ ký.

Locator trên URL chỉ resolve connection. Sau verify, kiểm tra tài khoản thụ hưởng thuộc binding và trusted tenant/merchant. Payload có code/content, amount, direction và external ID; adapter chuẩn hoá nhưng không tự quyết định quyền lợi của host.

ACK đúng contract SePay: HTTP 200/201 và JSON `success: true` sau khi dữ liệu tiếp nhận đã được lưu bền. Lỗi database trước durable commit không trả thành công. Duplicate delivery đã ghi bền trả thành công mà không settle thêm lần nữa. Payload đã xác thực nhưng không sử dụng được cần đường quarantine/audit; chính sách phản hồi cụ thể phải đóng contract khi triển khai, không giả định mọi 2xx đều được provider chấp nhận.

Retry provider hữu hạn: tài liệu đã đọc nêu một lần đầu và bảy retry theo Fibonacci, tổng lịch khoảng 33 phút; ngưỡng quét dự phòng 5 giờ không phải cam kết gửi liên tục 5 giờ. Không bảo đảm ordering.

## Mã và webhook filters

Recognition template là cấp công ty SePay, filter là cấp từng webhook. Mẫu gồm prefix, suffix min/max, character type và trạng thái; xem [payment-reference.md](payment-reference.md). Không đăng ký từng intent/code với provider theo cơ chế tài liệu này. Provider trích code từ giao dịch ngân hàng.

Khi “chỉ gửi khi có mã” bật, giao dịch không trích code sẽ không tới endpoint. Parser nội dung dự phòng chỉ áp dụng nếu payload thực sự đã được gửi. Giữ reconciliation để bù khoảng trống này.

## API đối soát

Adapter TransactionReader đọc theo cửa sổ có chồng lấn hoặc cursor và phân trang, tôn trọng rate limits theo kết nối. Tài liệu hiện đã đọc nêu API v2 giao dịch dùng UUID, pagination tối đa 100 và giới hạn 3 request/giây; xác minh lại contract/gói dịch vụ khi implement.

Không coi numeric webhook ID bằng UUID API. Nếu không chứng minh được identity, mở review, không tự tạo thêm settlement. Ghi checkpoints sau xử lý bền vững, không trước. Retry giới hạn tốc độ phải theo từng merchant/provider để một merchant lỗi không chặn tất cả.

## Merchant onboarding

Host xác nhận quyền sử dụng tài khoản và binding trước khi enable. Thiết lập endpoint, HMAC secret, tài khoản/VA và mẫu mã; Test/Live độc lập. Lưu bằng chứng cấu hình đã kiểm tra, không gắn nhãn live verified chỉ vì config local hợp lệ. Không có bằng chứng API tự đồng bộ mẫu ở tài liệu đã đọc; quy trình hiện đề xuất là thủ công.
