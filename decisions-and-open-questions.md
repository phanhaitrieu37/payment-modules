# Quyết định, đánh đổi và câu hỏi mở

[← Mục lục](readme.md)

## Người dùng đã quyết định

- Tư vấn kiến trúc Payment dùng lại; restaurant chỉ là case tích hợp.
- Mỗi project cài module và tự vận hành, chủ yếu Python.
- Một project hỗ trợ nhiều đơn vị nhận tiền ngay từ đầu.
- Khách chuyển khoản trực tiếp vào tài khoản merchant; không thu hộ/payout.
- Prefix cấu hình tùy project; đưa cơ chế mẫu mã SePay vào kiến trúc.
- Tài liệu/sơ đồ được public qua Orca và nay xuất đầy đủ sang thư mục riêng.

## Người dùng đã quyết định (21/09/2026)

Các quyết định này thay thế khuyến nghị/mặc định cũ ở những chỗ mâu thuẫn.

| # | Quyết định |
|---|---|
| U7 | Thêm hai bảng `PROVIDER_OBSERVATION` (mỗi bản tin webhook/dòng API một dòng) và `CONNECTION_ACCOUNT_BINDING` (một connection theo dõi nhiều tài khoản/VA của cùng merchant, cùng environment). |
| U8 | Năm kịch bản SePay Test (a–e) do codex agent chạy qua orca CLI. |
| U9 | v1 không chấp nhận thiếu/thừa tiền: không đường nào, kể cả operator, settle giao dịch có số tiền khác intent. |
| D1 | Prefix cấp project, **nhiều prefix có tên** trong một version `ReferenceProfile` (thay câu chữ cũ "một prefix mỗi project"). Mỗi prefix 2–5 chữ `A–Z`; mọi prefix của version chung `suffix_length` và alphabet; `payment_reference` unique toàn project; trong một version từ chối prefix trùng/lồng nhau, giữa các version chỉ advisory; checklist SePay có một mẫu và một bộ lọc cho mỗi prefix có tên, mẫu suffix min/max suy ra từ mọi version còn accepted dùng cùng prefix; readiness theo connection × profile version × environment; `CreateIntent` nhận tên prefix, tên lạ → lỗi. |
| D3 | Cú pháp orca theo đúng `--help` khi điều phối kịch bản SePay Test. |
| D4 | Test chống trùng kỳ vọng đúng **1 observation webhook + 1 observation API**, 1 provider transaction, 1 settlement. |
| D5 | Locator không tồn tại hoặc connection `disabled` → **404** đồng nhất; chữ ký/timestamp sai vẫn 401. |
| D6 | `TENANT_MISMATCH` là `ReviewReason` thứ 10: alert + mở review + metric (không còn chỉ inbox `failed`). |
| N1 | MeowAI là tham chiếu hành vi chỉ đọc; package viết mới từ thiết kế, không copy/port code. |
| N2 | Tích hợp/cutover MeowAI ra khỏi plan xây package, là plan riêng. D2 và D7 là quyết định MeowAI-specific, **hoãn** sang plan tích hợp tương lai. |

## Người dùng đã quyết định (22/09/2026)

- **Một chủ sở hữu tài khoản mỗi environment:** một tài khoản ngân hàng/VA thuộc đúng một `tenant + merchant` trong mỗi environment (Test/Live) của một bản cài. DB bảo đảm bằng `UQ(environment, account_fingerprint)` và composite FK; đóng gate sở hữu trước khi đóng băng schema. Xem [ma trận sở hữu](data-model.md#ma-trận-sở-hữu-ràng-buộc-và-test).
- **Kịch bản SePay Test đã chạy 22/09/2026** (run `sepay-test-20260922-1736`, chỉ Test, chỉ ACB): a INCONCLUSIVE (5/20 cặp), b PASS (`refreshed`), c INCONCLUSIVE, d PASS (kết quả âm), e PASS. Quyết định ở [ADR bằng chứng](plans/reports/sepay-evidence-decisions-260923.md): giữ `reconcile_mode = detect_only`, cửa sổ timestamp 300 giây, whitelist `transferType`, `account_key`; `auto_settle` không bật được khi thiếu `reconcile_evidence_ref` PASS.
- H3 default (22/09/2026): v1 does not transfer account ownership; UQ(environment, account_fingerprint) stays full (retired accounts keep their fingerprint reserved); a later partial unique index WHERE status <> 'retired' is a one-index migration.

## Quyết định thiết kế đi kèm

- **Bỏ cột `variance_vnd`.** Với U9 cột này luôn bằng 0 — dữ liệu chết, tự nó không chứng minh số tiền thật khớp. Thay bằng `SETTLEMENT.amount_vnd` và `intent_amount_vnd` (`NOT NULL`), `CHECK (amount_vnd = intent_amount_vnd)` và composite FK cùng `tenant + environment + receiving_account + số tiền` tới fact và intent, nên DB tự chặn sai tiền, sai receiver, chéo tenant/merchant/environment với mọi origin. Khi sản phẩm muốn nhận thiếu/thừa, một migration bỏ CHECK và thêm variance là thay đổi có chủ đích.
- **`ReviewResolution` v1:** `attach_to_intent` (chỉ intent đúng số tiền), `mark_external`, `mark_duplicate_of`, `bind_receiver`, `accept_late` (chỉ khi đúng số tiền).
- Tách quan sát khỏi fact tiền, liên kết hai nguồn bằng mã tham chiếu ngân hàng, `detect_only` mặc định; tiền ra là fact `not_applicable`; receiver lệch là review `RECEIVER_UNBOUND`; inbox bỏ `ignored`, thêm `quarantined`; bỏ `PaymentService`, dùng use case riêng + `MatchTransaction`.

## Phương án thiết kế đang dùng

Ports & Adapters, interface nhỏ/composition, PostgreSQL/SQLAlchemy adapter, FastAPI tùy chọn, HMAC, durable inbox, settlement/outbox atomic, exact-amount + review, handler cùng UoW hoặc async. Tên API/bảng và default cụ thể chưa phải code contract đã phát hành.

Versioned ReferenceProfile thay prefix-only: ghi rõ các prefix có tên, suffix/alphabet/version, snapshot và readiness merchant. Giả định dashboard SePay được cấu hình đúng; rủi ro đầu tiên là drift hoặc template overlap. Chi phí là thêm config history/checklist và kiểm tra cutover.

Không có phương án tốt hơn được chứng minh cho mục tiêu đã chốt. Prefix-only đơn giản hơn nhưng chuyển complexity thành quy ước ẩn. Autoprovision có thể tốt hơn nếu có API chính thức đủ quyền/khả năng kiểm tra; hiện chưa có chứng cứ. Đổi provider hoặc framework thông qua adapter; chuyển sang nhiều ngôn ngữ có chi phí API service/port và versioning.

## Không đưa vào scope

Marketplace plugin động, microservice bắt buộc, thu hộ/split/payout, restaurant infrastructure, e-invoice/VAT và subscription trong core, partial/refund như capability đã cam kết, nhận thiếu/thừa tiền. Không đặt tên recurring/refund method giả chỉ để interface trông tổng quát.

**Tích hợp MeowAI (N2)** là plan riêng; plan xây package chỉ chứng minh tái sử dụng bằng hai host mẫu cài cùng một wheel.

## Nguồn tham chiếu

Thiết kế được viết từ bộ tài liệu này, tài liệu SePay/NAPAS/EMVCo ([sources.md](sources.md)) và các báo cáo tư vấn trong `plans/reports/`.

> **Tham chiếu hành vi (chỉ đọc, không sao chép — quyết định N1 21/09/2026).** MeowAI (`git -C /Users/trieuphan/source_code/stk-meowai/MeowAI show origin/main:<path>`) dùng để đối chiếu *hành vi và biên ca*, không phải nguồn code. Mọi code, hằng số, tên hàm, docstring, bảng dữ liệu test trong package được viết từ tài liệu thiết kế + tài liệu SePay/NAPAS/EMVCo. Không copy-paste, không "port từng hàm", không import, không ghi output của MeowAI làm hằng số test nếu chưa được kiểm chéo bằng nguồn độc lập. Lý do: package phát hành độc lập, ranh giới sở hữu rõ (MeowAI là repo riêng: gốc DeerFlow MIT + code riêng của chủ dự án); reviewer từ chối PR có đoạn giống nguyên văn.

## Câu hỏi và bằng chứng còn thiếu

Đã giải quyết bằng thiết kế (không còn chặn schema): quan hệ một connection với nhiều tài khoản (binding n-n), provenance nhiều delivery cùng fact (observation), scope sở hữu tài khoản (một chủ mỗi environment), trạng thái kết thúc của mọi fact.

Còn mở, chờ SePay Test hoặc quyết định sản phẩm:

1. `referenceCode` (webhook) có luôn bằng `reference_number` (API) và không rỗng cho cùng giao dịch? SePay Test: 8/8 cặp ACB bằng nhau, không rỗng — còn mở tới khi có ≥20 cặp mỗi gateway. Quyết định lúc bật `auto_settle`, không đổi schema.
2. ~~Tên trường tài khoản nhận trong payload thật~~ — đã xác minh trên ACB: `bank|account_number|sub_account` khớp giữa webhook và API cho tài khoản chính và VA; còn mở cho gateway khác.
3. Regex boundary của SePay: overlong suffix, embedded code, adjacent characters, multiple codes.
4. ~~Timestamp khi retry và `webhook_success`~~ — đã trả lời: retry mang timestamp làm mới (header chậm ~152 giây, giữ 300 giây); `webhook_success` = 1 cả khi webhook chưa từng gửi, nên không phải tín hiệu delivery (chỉ dùng grace). Lịch retry 1 + 7 lần của tài liệu chưa quan sát (chỉ thấy 2 lần cách 62 giây).
5. Suffix default, retry budget collision và legacy retirement window.
6. API quản trị template có tồn tại/phù hợp không? Hiện không hứa auto-sync.
7. Retention raw payload/PII, throughput, rate-limit fairness và RPO/RTO.
8. Host nào chọn UoW và host nào async (mỗi host tự chọn; hai host mẫu minh hoạ cả hai).

Các câu hỏi này không chặn việc đọc bản thiết kế hay viết schema, nhưng cần giải quyết trước các phần production phụ thuộc chúng.
