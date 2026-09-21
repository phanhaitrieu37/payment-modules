# Payment module — đề xuất kiến trúc (nguồn cục bộ)

Ngày: 2026-09-21 (Asia/Saigon). Trạng thái: đề xuất thiết kế, chưa triển khai. Bản public: [Orca Artifacts](https://share.onorca.dev/a/uocuGxDkRWhN). Đây là bản chụp thiết kế nguồn; xem [readme.md](readme.md) để đọc bộ tài liệu đã tổ chức.

## Hợp đồng brainstorm

- **Outcome:** kiến trúc và sơ đồ public mô tả mẫu mã thanh toán cấu hình theo project; multi-merchant nhận tiền thẳng vào tài khoản merchant giữ nguyên.
- **Constraints:** tách hành vi SePay đã xác minh (S17) khỏi quyết định đề xuất; tiền tố không bỏ qua được auth, tenant, receiver, dedup; không hứa auto-sync mẫu hay đăng ký mã theo intent.
- **Non-goals:** triển khai code, sửa cấu hình SePay thật, thiết kế hệ thống nhà hàng, mở rộng payout/refund.
- **Acceptance:** tài liệu, mô hình, ports, sequence, pipeline và ma trận bằng chứng nhất quán; có sơ đồ D13 cho vòng đời mẫu mã và xoay version; SVG/PNG render với hash khớp; trang public tự chứa, đã sanitize, cập nhật tại URL cũ.

## Kết luận

Package Python độc lập, mỗi dự án tự cài, tự vận hành và pin version. Lớp Domain / Application / Ports; SePay, SQLAlchemy/PostgreSQL và FastAPI là adapter. Mỗi dự án có nhiều merchant, khách chuyển khoản thẳng vào tài khoản của merchant. Package không thu hộ, không chia tiền, không payout.

## Phạm vi

- Trong phạm vi: package, provider SePay (xác thực, chuẩn hoá, đối soát), multi-merchant, intent / giao dịch / settlement / inbox / outbox / review, adapter PostgreSQL, router FastAPI tuỳ chọn.
- Ngoài phạm vi: thu hộ/chia tiền/payout, service trung tâm dùng chung, thiết kế hệ thống nhà hàng (LAN, edge, bếp, stack), e-invoice/VAT/subscription/credit trong lõi, refund và partial payment như tính năng đã hứa.
- Restaurant và MeowAI chỉ là ví dụ tích hợp ngắn. Bản public dùng nhãn chung “SaaS” và “F&B”.

## Bất biến chính

1. Webhook chỉ được tin sau khi HMAC-SHA256 trên `{timestamp}.{raw_body}` hợp lệ với secret của chính connection. Locator trên URL chỉ để định tuyến. Locator không tồn tại hoặc connection `disabled` → **404 đồng nhất**, không lộ connection nào tồn tại; chữ ký/timestamp sai → 401 (quyết định người dùng D5, 21/09/2026).
2. Guard lõi chạy trước MatchingPolicy: xét chiều tiền trước (ra/`unknown` → `not_applicable`), rồi receiver phải thuộc binding của connection (cùng merchant, cùng environment). Sau resolver, lõi kiểm scope của intent tìm được: khác tenant, khác environment hoặc khác tài khoản nhận → `TENANT_MISMATCH` với `details.scope`: **alert + mở review + metric**, không settle; đây là `ReviewReason` thứ 10 (quyết định người dùng D6, 21/09/2026). Policy tuỳ biến không nới được các điều kiện này.
3. Ghi inbox bền vững rồi mới ACK. DB lỗi thì trả non-2xx để SePay retry (retry là hữu hạn).
4. Unique trong DB: inbox `(connection, event_key)`, giao dịch `UQ(tenant_id, environment, dedup_key)` theo định danh tài khoản ổn định do payload báo, settlement theo giao dịch và theo intent.
5. Fact ngân hàng luôn được lưu, độc lập với kết quả matching. Unmatched được lưu bền và rematch được.
6. Intent bất biến về merchant, tài khoản thụ hưởng và số tiền. Đổi số tiền thì tạo intent mới.
7. Mặc định chỉ khớp đúng số tiền. Lệch tiền hoặc trả muộn thì vào review.
8. Settlement, trạng thái intent và outbox commit cùng một transaction. Phương án A: handler của host chạy cùng UoW. Phương án B: outbox + fulfillment idempotent có theo dõi và retry.
9. Ba lớp tách biệt: giao dịch ngân hàng đã xác thực, intent đã khớp, hành động nghiệp vụ đã hoàn tất. Fulfillment lỗi không đảo trạng thái paid.
10. Không đánh dấu đã trả khi tạo QR hay khi browser quay lại. Không tuyên bố exactly-once qua mạng.
11. Tiền tố mã thanh toán không phải xác thực: HMAC, receiver, tenant và dedup không bị bỏ qua nhờ tiền tố.

## Mã thanh toán theo project (cập nhật 21/09/2026)

Quyết định người dùng đã chốt: tiền tố cấu hình ở **cấp project**, không bao giờ theo merchant. Quyết định D1 (21/09/2026) thay câu chữ cũ "một prefix mỗi project": một version `ReferenceProfile` chứa **nhiều prefix có tên** (vd `subscription`/`topup`), mỗi prefix 2–5 chữ `A–Z`, dùng chung `suffix_length` và alphabet; trong một version không có prefix trùng/lồng nhau; checklist SePay có một mục mẫu và một mục bộ lọc cho mỗi prefix có tên; `CreateIntent` nhận tên prefix. Multi-merchant và chuyển khoản thẳng vào tài khoản merchant giữ nguyên. Sơ đồ D13; chi tiết ở [payment-reference.md](payment-reference.md).

**Hành vi SePay đã đọc (S17, https://developer.sepay.vn/vi/sepay-webhooks/cau-hinh-ma-thanh-toan):** cấu hình ở cấp công ty, có công tắc nhận diện toàn cục (tắt → `code` rỗng). Tiền tố 2–5 ký tự, lưu thành chữ hoa, so khớp không phân biệt hoa/thường; tài liệu không nói rõ bảng ký tự hợp lệ của tiền tố. Hậu tố min/max trong 1–30, max ≥ min, mặc định 6/8; kiểu số 0–9 hoặc số+chữ A–Z0–9. Nhiều mẫu đang bật được duyệt theo thứ tự khai báo, mẫu khớp đầu tiên thắng. Mẫu đầu tiên (mặc định) không xoá được, chỉ tắt được. Webhook có bộ lọc riêng: chỉ gửi khi có mã, lọc theo tiền tố của mã đã trích. Test và Live độc lập. Không có bằng chứng về API tự tạo mẫu hay đăng ký mã theo intent.

**Đề xuất:**
- `ReferenceProfile` bất biến, có version: nhiều prefix có tên (chỉ ASCII A–Z, 2–5 ký tự), hậu tố độ dài cố định và bảng ký tự dùng chung cho mọi prefix của version. Trên SePay, mẫu của một prefix dùng suffix min/max phủ mọi version còn accepted dùng cùng prefix. 12 ký tự chỉ là ví dụ mặc định, chưa chốt, không phải bảo đảm của provider.
- `PaymentReferenceGenerator` (port) + `RandomSuffixGenerator` (strategy): hậu tố ngẫu nhiên, unique DB, sinh lại khi trùng; không phải token bảo mật.
- `ReferenceTemplateChecklist` (port provider) + adapter SePay: kiểm profile có hợp lệ với ràng buộc SePay và liệt kê checklist; không tự cấu hình SePay.
- Intent snapshot `payment_reference` + `reference_profile_version` + `reference_prefix_name`. Reference unique trong project; resolver tra mã toàn project rồi kiểm scope tenant/environment/tài khoản nhận (lệch → `TENANT_MISMATCH`). Snapshot cũ không bao giờ bị rewrite.
- `CONNECTION_REFERENCE_READINESS`: checklist onboarding mỗi connection — áp profile vào mẫu mã cấp CÔNG TY SePay của merchant (không phải nhận diện theo webhook), binding tài khoản, bộ lọc từng webhook; trạng thái ready dựa trên bằng chứng xác minh thủ công, không phải chứng thực từ xa. Không có control plane tự động.
- Xoay: một version active mỗi project, các version lịch sử vẫn được chấp nhận. Chỉ cutover khi mọi connection liên quan đã ready. Giữ mẫu nhận diện VÀ bộ lọc webhook của version cũ cho intent cũ/tiền muộn; không gỡ chỉ vì intent hết hạn.
- Khớp nguyên reference ở server; không có ứng viên → review `NO_REFERENCE`, nhiều ứng viên mâu thuẫn → review `AMBIGUOUS_REFERENCE`. Parser nội dung dự phòng chỉ sau xác thực + kiểm tra receiver; không cứu được webhook đã bị SePay lọc — việc đó thuộc đối soát API.
- Nhiều project độc lập dùng chung tài khoản nhận: tiền tố rời nhau + ownership rõ khi onboarding; DB cục bộ không bảo đảm unique giữa các bản cài.

**So sánh:** (1) Chỉ tiền tố — loại. Nó không tự buộc hậu tố có độ dài thay đổi, nhưng để ngầm định độ dài/bảng ký tự của hậu tố và chính sách xoay, nên generator, mẫu SePay của từng merchant và bộ lọc webhook phải khớp nhau mà không có chỗ nào ghi lại (coupling ẩn); khi đổi thì không biết mã nào thuộc cấu hình nào. (2) Profile đầy đủ có version — **chọn**: ghi tường minh tiền tố, độ dài, bảng ký tự và version cho mỗi intent. Chi phí ước tính: lưu profile và trạng thái sẵn sàng theo connection (đề xuất hai bảng; đây là đề xuất thiết kế, không phải yêu cầu của người dùng), 1 port sinh mã, 1 adapter checklist, onboarding thủ công. Hỏng nếu mẫu công ty của merchant lệch profile hoặc một mẫu khác đứng trước và khớp trước. (3) SePay tự provision — loại, không có bằng chứng API.

**Kết luận về phương án tốt hơn:** không thấy phương án nào tốt hơn profile đầy đủ có version trong phạm vi bằng chứng hiện có. Chỉ tiền tố rẻ hơn nhưng dồn rủi ro vào quy ước ngầm; autoprovision sẽ tốt hơn về vận hành nếu SePay công bố API, khi đó chỉ cần thay adapter checklist mà không đổi domain.

## Hiện trạng quan sát ở dự án nguồn (chưa kiểm tra lại khi xuất tài liệu)

Đã đọc `backend/app/gateway/routers/sepay_webhooks.py` và `backend/packages/harness/deerflow/persistence/payment/model.py`. Code hiện có verify HMAC trên raw body trước khi parse, kiểm tra timestamp trong ngưỡng 300 giây, và dùng unique partial index `uq_payment_tx_sepay_id`. Nó khớp theo whole token của mã đơn cộng đúng số tiền, nếu không thì đưa vào `unmatched`. Tiền, trạng thái đơn và entitlement được ghi trong một transaction; chỉ trả 500 khi DB lỗi tạm thời.

Khoảng trống so với đề xuất: một secret toàn cục trong env, một merchant, unique của giao dịch chưa theo tài khoản, xử lý inline chưa có inbox/outbox, và order gắn với user/plan. Payload đang dùng các trường `id`, `transferAmount`, `content`, `code`, `transferType`. Trường chứa tài khoản nhận chưa được code dùng, nên tên trường vẫn còn mở.

## Quyết định còn mở

- Ánh xạ ID số của webhook với UUID của API v2 (S3, S4): chưa xác minh.
- Trường tài khoản nhận trong payload và định danh tài khoản phía SePay dùng làm `provider_account_key`.
- Biên khớp mã của SePay (hậu tố dài hơn max, mã lọt trong chuỗi dài hơn, ký tự liền kề) chưa được tài liệu mô tả: thử ở Test mode và với nội dung thật của ngân hàng trước production. Độ dài hậu tố mặc định chưa chốt.
- API tự cấu hình mẫu mã: không có bằng chứng; onboarding thủ công.
- Partial payment, chia bill, refund; chính sách review; thời hạn giữ raw payload.
- Mỗi dự án chọn phương án A hay B.

## Sơ đồ

Bản Markdown tự chứa: [diagrams.md](diagrams.md).

| ID | File | Loại | Cách dựng |
|---|---|---|---|
| D01 | diagrams/01-architecture-overview.md | flowchart ELK | viết tay |
| D02 | diagrams/02-component-dependencies.md | flowchart ELK | viết tay |
| D03 | diagrams/03-ports-classes.md | classDiagram | viết tay |
| D04 | diagrams/04-entity-relationship.md | erDiagram | viết tay |
| D05 | diagrams/05-multi-merchant-trust.md | flowchart ELK | viết tay |
| D06 | diagrams/06-sequence-success.md | sequenceDiagram | viết tay |
| D07 | diagrams/07-sequence-failure-retry.md | sequenceDiagram | viết tay |
| D08 | diagrams/08-sequence-reconcile.md | sequenceDiagram | viết tay |
| D09 | diagrams/09-state-payment-intent.md | stateDiagram-v2 (dagre) | viết tay |
| D10 | diagrams/10-state-inbox-transaction.md | stateDiagram-v2 ELK | viết tay |
| D11 | diagrams/11-processing-pipeline.md | flowchart ELK | compile-pipeline.py từ evidence/pipeline-input.json, sau đó thêm dòng font |
| D12 | diagrams/12-host-integration.md | flowchart ELK | viết tay |
| D13 | diagrams/13-reference-pattern-lifecycle.md | flowchart ELK | viết tay |

Bằng chứng đã chuyển sang [evidence-matrix.md](evidence-matrix.md).

## Nguồn

Danh mục URL độc lập tại [sources.md](sources.md). S1–S16 trong nghiên cứu nguồn — S1–S4 (SePay), S7 (Redis), S8 (AWS outbox), S15 (PyPA), S16 (pluggy). S17: https://developer.sepay.vn/vi/sepay-webhooks/cau-hinh-ma-thanh-toan, đọc trực tiếp ngày 21/09/2026.
