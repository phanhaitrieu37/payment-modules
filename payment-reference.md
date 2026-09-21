# Prefix và mẫu mã thanh toán theo project

[← Mục lục](readme.md) · [D13: luồng đầy đủ](diagrams/13-reference-pattern-lifecycle.md)

## Quyết định

Prefix do project cấu hình, không đặt riêng theo merchant. Module tạo mã đầy đủ và lưu vào intent; khách chuyển khoản thẳng tới tài khoản đã chọn. Prefix phục vụ nhận diện/lọc, không phải secret hay chứng cứ thanh toán.

## Hành vi SePay đã xác minh qua tài liệu

| Thuộc tính | Ràng buộc |
|---|---|
| Prefix | 2–5 ký tự, uppercase khi lưu, match không phân biệt hoa/thường |
| Suffix min/max | Mỗi giá trị 1–30, max ≥ min; mặc định 6/8 |
| Suffix charset | Số 0–9 hoặc chữ và số A–Z0–9 |
| Recognition | Công tắc toàn cục cấp công ty; tắt thì code rỗng |
| Templates | Nhiều mẫu active, duyệt theo thứ tự, mẫu khớp đầu tiên thắng |
| Default template | Không xoá được, có thể tắt |
| Webhook filters | Chỉ gửi khi có code; chỉ gửi code thuộc prefix đã chọn |
| Environment | Test và Live riêng |

Nguồn: [SePay — cấu hình mã thanh toán](https://developer.sepay.vn/vi/sepay-webhooks/cau-hinh-ma-thanh-toan), đã đọc 21/09/2026. Tài liệu không xác định rõ toàn bộ tập ký tự prefix hoặc regex boundary.

## Profile đề xuất

Mỗi version có prefix, suffix_length cố định và alphabet. ASCII A–Z cho prefix là restriction đề xuất của module, không gán nhầm thành quy định provider. Ví dụ suffix dài 12 chỉ để minh họa, chưa được chốt làm default sản phẩm. Sinh phần suffix ngẫu nhiên, có unique constraint và retry hữu hạn khi collision; không hứa xác suất trùng bằng zero.

Intent snapshot mã đầy đủ và profile version. Một active generation version theo project, nhiều accepted legacy version. Profile value bất biến sau sử dụng; lifecycle status thay đổi có audit. Persistence hai bảng profile/readiness là đề xuất, không bắt buộc phải là hình dạng schema cuối.

## Luồng thiết lập và sinh mã

1. Project xác định mẫu, module validate các giới hạn provider đã biết.
2. Áp mẫu tương ứng ở từng công ty SePay liên quan; bật recognition và kiểm tra thứ tự mẫu không chồng lấn.
3. Chọn bộ lọc từng webhook và tài khoản/VA đúng merchant.
4. Test code trích được, tài khoản, amount và endpoint bằng Test mode, sau đó xác minh Live theo quy trình được cho phép.
5. Ghi readiness theo connection/profile/môi trường dựa trên bằng chứng thủ công.
6. Generator dùng active profile, lưu mã với intent rồi trả QR/chỉ dẫn.
7. Khi webhook đến, verify/guard trước; đối chiếu toàn bộ reference và receiver, không chỉ startswith(prefix).

Nhiều mã mâu thuẫn hoặc code và content cho ứng viên khác nhau phải review, không lấy kết quả đầu tiên một cách mù quáng. Quy tắc normalize phải bảo toàn tính không mơ hồ; không tự xoá ký tự bất kỳ để ép khớp.

## Rotation

Tạo profile mới → cấu hình cả mẫu recognition và filter mới trên các connection liên quan → xác minh readiness → cutover sinh mã mới. Mã cũ không rewrite. Giữ mẫu và filter cũ cho pending intents và late payments; intent hết hạn không đủ để tự xoá mẫu cũ. Chính sách kết thúc hỗ trợ mã cũ phải có thời hạn nghiệp vụ và đường đối soát/review.

Nếu hai project độc lập dùng chung tài khoản nhận, chọn prefix không chồng lấn và ownership rõ. Project-wide unique trong một DB không bảo đảm uniqueness giữa các DB. Prefix rotation trên cùng SePay company có thể ảnh hưởng nhiều webhook/project, phải đánh giá trước.

## So sánh

Chỉ-prefix rẻ hơn nhưng ngầm hoá suffix/rotation, dễ lệch với provider. Full profile ghi rõ contract, tăng chi phí quản lý phiên bản và readiness, là phương án đã chọn. Auto-provision hấp dẫn về vận hành nhưng chưa có API được chứng minh; chưa đưa vào khả năng cam kết. Full profile vẫn thất bại nếu cấu hình dashboard bị sửa lệch hoặc mẫu trước bắt mã; cần evidence và đối soát, không có bảo đảm từ validate local.

## Các test còn cần

Hậu tố ngắn/dài hơn giới hạn, prefix lồng nhau, mã nằm liền trong chuỗi khác, nhiều mã trong memo, chữ thường, ngân hàng sửa nội dung, recognition tắt, filter bỏ giao dịch, version cũ sau cutover. Không suy diễn regex exact-token của SePay từ ví dụ ngắn trong docs.
