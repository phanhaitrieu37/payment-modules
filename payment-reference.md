# Prefix và mẫu mã thanh toán theo project

[← Mục lục](readme.md) · [D13: luồng đầy đủ](diagrams/13-reference-pattern-lifecycle.md)

## Quyết định

Prefix cấu hình ở **cấp project**, không bao giờ đặt riêng theo merchant. Một version `ReferenceProfile` chứa **nhiều prefix có tên** (quyết định người dùng D1, 21/09/2026), ví dụ `subscription → SUB`, `topup → TOP`; host chọn loại bằng tên khi gọi `CreateIntent(prefix_name=...)`, tên lạ → lỗi `UnknownReferencePrefix`. Module tạo mã đầy đủ và lưu vào intent; khách chuyển khoản thẳng tới tài khoản đã chọn. Prefix phục vụ nhận diện/lọc, không phải secret hay chứng cứ thanh toán.

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

Mỗi version có **một hoặc nhiều prefix có tên** (bảng con `REFERENCE_PROFILE_PREFIX`), cùng một `suffix_length` cố định và một alphabet dùng chung cho mọi prefix của version. Mỗi prefix 2–5 chữ `A–Z` (giới hạn SePay; ASCII A–Z là restriction của module, không gán nhầm thành quy định provider). Tên prefix `^[a-z][a-z0-9_]{0,31}$`. Ví dụ độ dài suffix chỉ để minh họa, chưa được chốt làm default sản phẩm. Sinh phần suffix ngẫu nhiên, có unique constraint và retry hữu hạn khi collision; không hứa xác suất trùng bằng zero.

Luật chồng lấn prefix:

1. **DB:** `UQ(payment_reference)` toàn project; `UQ(profile_version, prefix)`; CHECK độ dài 2–5.
2. **Trong một version:** tên prefix không trùng; giá trị prefix không trùng và không lồng nhau (`SUB`/`SUBX`) → lỗi `PrefixOverlap`. Lý do: cùng `suffix_length` nên mã của hai prefix lồng nhau có tổng độ dài khác nhau, dễ phụ thuộc thứ tự mẫu SePay.
3. **Giữa các version còn được chấp nhận** (`active`, `accepted_legacy`, `kind=generated`): **không** từ chối. `check_prefix_overlap` chỉ trả advisory kèm hình mã `(prefix, suffix_length, alphabet)` của từng bên. Mã unique toàn project và khớp nguyên token nên một chuỗi chỉ thuộc một intent/một version.
4. **Checklist SePay** suy ra từ mọi version còn chấp nhận dùng cùng prefix: mẫu nhận diện của prefix đó có suffix **min = min(L_i), max = max(L_i)**, charset là hợp các alphabet (số ⊂ chữ+số); khi có lồng nhau giữa các version, prefix dài hơn đứng trước. Bộ lọc webhook theo prefix không đổi.

Intent snapshot mã đầy đủ, profile version và tên prefix. Một active generation version theo project, nhiều accepted legacy version. Profile value bất biến sau sử dụng; lifecycle status thay đổi có audit. Kích hoạt version mới là **một transaction** (mới `active`, cũ `accepted_legacy`), luôn đúng một active. Profile `legacy_import` không có prefix, không sinh mã, chỉ để nhận mã cũ của host.

**Mã cũ/do host cấp (`reference_override`).** Host có mã cũ không theo profile tạo intent với `reference_override`: chỉ đi cùng một profile `kind=legacy_import` đang `accepted_legacy` (dấu hiệu mã do host cấp), `reference_prefix_name = NULL`, không validate theo luật sinh mã (prefix/suffix/alphabet). Mã vẫn phải là một token đã normalize và `UQ(payment_reference)` toàn project vẫn áp dụng: trùng với mã đã có thì conflict, không ghi đè. Schema không cần cột mới: `reference_prefix_name NULL` + `FK(reference_profile_version)` tới profile `legacy_import`; việc buộc name NULL chỉ với profile legacy do application kiểm.

Readiness theo **connection × profile version × environment**. Checklist gồm một mục mẫu nhận diện cấp company của SePay và một mục bộ lọc webhook **cho mỗi prefix có tên**, cộng các mục chung (bật nhận diện, binding tài khoản/VA). Readiness thiếu mẫu hoặc bộ lọc của bất kỳ prefix có tên nào không được nhận.

## Luồng thiết lập và sinh mã

1. Project xác định các prefix có tên, suffix và alphabet; module validate giới hạn provider đã biết và luật chồng lấn trong version.
2. Áp một mẫu cho mỗi prefix có tên ở từng công ty SePay liên quan; bật recognition và kiểm tra thứ tự mẫu không chồng lấn.
3. Chọn bộ lọc từng webhook cho mỗi prefix có tên và tài khoản/VA đúng merchant.
4. Test code trích được, tài khoản, amount và endpoint bằng Test mode, sau đó xác minh Live theo quy trình được cho phép.
5. Ghi readiness theo connection/profile version/environment dựa trên bằng chứng thủ công.
6. Host gọi `CreateIntent` với tên prefix; generator dùng active profile, lưu mã với intent rồi trả QR/chỉ dẫn.
7. Khi webhook đến, verify/guard trước; đối chiếu toàn bộ reference và receiver, không chỉ startswith(prefix).

Nhiều mã mâu thuẫn hoặc code và content cho ứng viên khác nhau phải review, không lấy kết quả đầu tiên một cách mù quáng. Quy tắc normalize phải bảo toàn tính không mơ hồ; không tự xoá ký tự bất kỳ để ép khớp.

## Rotation

Tạo profile mới → cấu hình cả mẫu recognition và filter mới trên các connection liên quan → xác minh readiness → cutover sinh mã mới trong một transaction. Mã cũ không rewrite. Giữ mẫu và filter cũ cho pending intents và late payments; intent hết hạn không đủ để tự xoá mẫu cũ. Chính sách kết thúc hỗ trợ mã cũ phải có thời hạn nghiệp vụ và đường đối soát/review.

Ví dụ giữ prefix, đổi độ dài: v1 `{subscription: SUB, topup: TOP}` suffix 24 → v2 **giữ nguyên hai prefix**, suffix 20. v2 tạo được (cùng prefix giữa hai version chỉ là advisory) và kích hoạt được khi readiness v2 `ready` trên mọi connection active. Intent mới ra mã 23 ký tự; webhook mang mã v1 (27 ký tự) vẫn settle. Checklist v2 in mẫu `subscription` là `SUB` suffix min 20 max 24 (tương tự `TOP`), phủ cả hai version.

Nếu hai project độc lập dùng chung tài khoản nhận, chọn prefix không chồng lấn và ownership rõ. Project-wide unique trong một DB không bảo đảm uniqueness giữa các DB. Prefix rotation trên cùng SePay company có thể ảnh hưởng nhiều webhook/project, phải đánh giá trước.

## So sánh

Chỉ-prefix rẻ hơn nhưng ngầm hoá suffix/rotation, dễ lệch với provider. Full profile ghi rõ contract, tăng chi phí quản lý phiên bản và readiness, là phương án đã chọn. Auto-provision hấp dẫn về vận hành nhưng chưa có API được chứng minh; chưa đưa vào khả năng cam kết. Full profile vẫn thất bại nếu cấu hình dashboard bị sửa lệch hoặc mẫu trước bắt mã; cần evidence và đối soát, không có bảo đảm từ validate local.

## Các test còn cần

Hậu tố ngắn/dài hơn giới hạn, prefix lồng nhau trong một version (lỗi) và giữa các version (advisory), tên prefix lạ khi tạo intent, checklist thiếu mẫu/bộ lọc của một prefix có tên, mã nằm liền trong chuỗi khác, nhiều mã trong memo, chữ thường, ngân hàng sửa nội dung, recognition tắt, filter bỏ giao dịch, version cũ sau cutover. Không suy diễn regex exact-token của SePay từ ví dụ ngắn trong docs.
