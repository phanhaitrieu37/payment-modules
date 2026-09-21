# Domain và mô hình dữ liệu

[← Mục lục](readme.md) · [ER diagram](diagrams/04-entity-relationship.md)

Trạng thái: đã đối chiếu với các delta đã duyệt và quyết định người dùng tới 22/09/2026 (xem [sổ quyết định](decisions-and-open-questions.md)). Hình dạng bảng và ràng buộc dưới đây là hợp đồng thiết kế cho `schema_v1`; chưa phải schema đã triển khai.

## Khái niệm

| Entity | Ý nghĩa và ràng buộc |
|---|---|
| Tenant | Scope của host, module không sở hữu vòng đời user/tenant |
| Merchant | Đơn vị nhận tiền, có host reference và trạng thái |
| ReceivingAccount | Tài khoản ngân hàng/VA của merchant trong một environment; identity ổn định `account_fingerprint = BANK\|ACCOUNT\|SUB` tính từ số đầy đủ; **một chủ sở hữu mỗi environment** trong một bản cài |
| ProviderConnection | Một webhook SePay của merchant trong một environment: locator, `secret_ref`, `api_credential_ref` (token API cấp company), `reconcile_mode`, `timestamp_tolerance_seconds`, trạng thái `pending/active/not_ready/disabled` |
| ConnectionAccountBinding | Tập tài khoản/VA một connection theo dõi (n-n), buộc cùng merchant và cùng environment |
| ReferenceProfile | Mẫu sinh mã cấp project có version, bất biến sau khi dùng; `suffix_length` và `alphabet` chung cho mọi prefix của version |
| ReferenceProfilePrefix | Prefix có tên của một version (D1), vd `subscription → SUB`, `topup → TOP`; 2–5 chữ `A–Z`; `UQ(profile_version, prefix)` |
| ConnectionReferenceReadiness | Bằng chứng thủ công cấu hình SePay phù hợp một profile, theo connection × profile version × environment; checklist có mẫu + bộ lọc cho mỗi prefix có tên |
| PaymentIntent | Amount integer VND, beneficiary snapshot, merchant, environment, `payment_reference`, `reference_profile_version`, `reference_prefix_name`, expiry, business reference, `superseded_by_intent_id` |
| WebhookInbox | Raw delivery đã xác thực, `event_key` (`webhook:<id>` hoặc `sha256:<hash>`), `body_sha256`, trạng thái xử lý, lease + generation, attempts, retry, `purge_after` |
| ProviderObservation | Mỗi lần *nhìn thấy* một giao dịch (một bản tin webhook hoặc một dòng API); giữ provenance, liên kết tới fact canonical khi đủ bằng chứng |
| ProviderTransaction | Fact tiền canonical (mỗi khoản tiền một dòng): `provider_account_key` do payload báo, `receiving_account_id` đã resolve (NULL khi chưa bind), identity nguồn, `match_state` |
| Settlement | Liên kết một fact với một intent: `amount_vnd`, `intent_amount_vnd` (bằng nhau), origin, provenance operator |
| ReviewCase | Khoản tiền cần kiểm tra, `ReviewReason`, người xử lý, `ReviewResolution` và dấu vết quyết định |
| OutboxEvent | Sự kiện đã commit cùng thay đổi domain, `schema_version`, trusted scope, lease + generation |
| ReconciliationRun | Lần đối soát: cursor `since_id`, cửa sổ, counts (linked/unlinked/ambiguous/new/review) |

Package có 15 bảng `pm_*`. Hai bảng `PROVIDER_OBSERVATION` và `CONNECTION_ACCOUNT_BINDING` là quyết định người dùng U7; `REFERENCE_PROFILE_PREFIX` là bảng con của D1.

## Quy tắc về tiền và identity

Amount VND là số nguyên, không dùng float để tính/so khớp. Intent snapshot số tiền, người thụ hưởng, mã, profile version và tên prefix bất biến. Thay số tiền tạo intent mới (`superseded_by_intent_id`); không sửa lịch sử để làm cho giao dịch khớp. Cùng idempotency key với payload khác trả conflict, không trả nhầm intent cũ.

**v1 không chấp nhận thiếu/thừa tiền (U9).** Không đường nào, kể cả operator, settle giao dịch có số tiền khác intent. `SETTLEMENT` không có cột `variance_vnd`: với U9 cột đó luôn bằng 0, là dữ liệu chết và không tự chứng minh số tiền thật khớp. Thay vào đó `SETTLEMENT` lưu `amount_vnd` và `intent_amount_vnd` (cả hai `NOT NULL`), `CHECK (amount_vnd = intent_amount_vnd)`, và hai composite FK mang `tenant + environment + receiving_account + số tiền` tới fact và tới intent. Nếu sản phẩm sau này muốn nhận thiếu/thừa, một migration bỏ CHECK và thêm variance là thay đổi có chủ đích.

Mặc định một giao dịch đúng tiền tất toán một intent. Unique/lock tại database bảo đảm không hai worker cùng settle. Ràng buộc khóa ngoại chặn liên kết chéo tenant, merchant và environment, không chỉ dựa vào filter ứng dụng.

Dedup dựa trên định danh tài khoản **do payload báo về** (`provider_account_key`), không dựa vào FK đã resolve, secret hay connection ID có thể đổi: `dedup_key = provider|provider_account_key|identity_kind|identity_value`. Fact của tài khoản chưa bind vẫn dedup đúng khi webhook gửi lại.

ID số của webhook và UUID của API v2 là hai không gian ID khác nhau (`webhook_tx_id`, `api_tx_id`, mỗi cột partial unique trong scope). Cầu nối hai nguồn là mã tham chiếu ngân hàng `bank_reference` (chỉ index thường, không unique). Chỉ link khi đủ bằng chứng; không dùng amount + thời gian gần nhau làm bằng chứng duy nhất cùng giao dịch, không gộp hai khoản tiền giống nhau khi identity chưa đủ.

## `ReviewReason` và `ReviewResolution`

`ReviewReason` có đúng **10** giá trị: `RECEIVER_UNBOUND`, `NO_REFERENCE`, `AMBIGUOUS_REFERENCE`, `TENANT_MISMATCH`, `ALREADY_PAID`, `INTENT_CANCELLED`, `INTENT_SUPERSEDED`, `AMOUNT_MISMATCH`, `LATE`, `UNVERIFIED_IDENTITY`. `TENANT_MISMATCH` (D6) là lý do thứ 10: mã khớp intent của tenant khác → alert + mở review trong tenant của fact + metric, `candidate_intent_id = NULL`, `details` không chứa id/tenant bên kia. `UNVERIFIED_IDENTITY` do Reconcile đặt, không phải policy.

`ReviewResolution` v1: `attach_to_intent` (chỉ intent đúng số tiền), `mark_external`, `mark_duplicate_of` (cần provenance có cấu trúc), `bind_receiver` (kích `RematchUnbound`), `accept_late` (chỉ khi đúng số tiền). Operator không bypass guard; mọi resolution có actor, reason và audit.

## Ba loại trạng thái độc lập

1. Bản tin đã được tiếp nhận/xử lý (inbox: `received`, `processing`, `processed`, `retry_wait`, `failed`, `quarantined`). Inbox không có trạng thái `ignored`; tiền ra vẫn là fact.
2. Fact tiền đã khớp intent hay cần review (`recorded → settled | in_review | not_applicable`; `in_review → settled | closed_external | duplicate_of`). Mọi fact có trạng thái kết thúc.
3. Quyền lợi của host đã được giao thành công hay đang retry.

Giao dịch unmatched vẫn tồn tại. Trong phương án async, host fulfillment lỗi không làm intent đã paid thành pending. Trong phương án cùng UoW, lỗi handler làm rollback cả quyết định settlement của transaction đó; inbox đã tiếp nhận vẫn còn để retry.

## Quy tắc sở hữu, provenance, rotation và recovery

- **Sở hữu (quyết định người dùng 22/09/2026):** một tài khoản/VA thuộc đúng một `tenant + merchant` trong mỗi environment (Test/Live) của một bản cài. Merchant của connection, receiving account và intent phải trùng nhau; cùng tenant chưa đủ.
- **Provenance:** observation và fact canonical nó link tới có cùng scope `tenant + environment + provider + provider_account_key`; hai connection chỉ cùng link một fact khi scope đó khớp.
- **Rotation:** kích hoạt profile mới là một transaction (version mới `active`, version cũ `accepted_legacy`, luôn đúng một active). Prefix, mẫu và bộ lọc của mọi version còn accepted được giữ; intent hết hạn không đủ để gỡ chúng.
- **Recovery:** claim, xử lý, ghi lỗi/retry và thu hồi lease hết hạn là các transaction tách biệt, có fencing bằng `lease_generation`.
- **Đường API:** fact tiền ra/unknown từ API không vào review. Fact chỉ có từ API mà không có thời gian provider đã xác minh không được auto-settle intent đã quá hạn.

## Ma trận sở hữu, ràng buộc và test

Mỗi dòng: quan hệ → guard ứng dụng → ràng buộc DB → test SQL trực tiếp dự kiến ở phase storage (insert bằng SQL Core, kỳ vọng `IntegrityError`). Tên bảng dùng prefix mặc định `pm_`. Test ở `tests/integration/storage/test_tenant_isolation_constraints.py` (scope) và `test_money_constraints.py` (tiền, trạng thái, profile).

| # | Quan hệ | Guard ứng dụng | Ràng buộc DB | Test SQL trực tiếp dự kiến |
|---|---|---|---|---|
| O1 | Merchant thuộc tenant | Host cấp tenant đã xác thực; không đọc tenant từ client | `pm_merchants UQ(tenant_id, id)`, `UQ(tenant_id, host_merchant_ref)` | trùng `host_merchant_ref` cùng tenant bị từ chối |
| O2 | Tài khoản/VA thuộc **một** tenant + merchant mỗi environment | `RegisterReceivingAccount` từ chối fingerprint đã có chủ trong environment | `pm_receiving_accounts UQ(environment, account_fingerprint)` (không scope tenant); `FK(tenant_id, merchant_id)`; `UQ(tenant_id, merchant_id, environment, id)` làm đích FK | cùng fingerprint + environment cho tenant khác, và cho merchant khác cùng tenant, đều bị từ chối; cùng fingerprint ở Test và Live được nhận |
| O3 | Connection thuộc merchant, một environment | `RegisterConnection` lấy environment từ cấu hình, không từ webhook | `pm_provider_connections FK(tenant_id, merchant_id)`; `UQ(locator)`; `UQ(tenant_id, merchant_id, environment, id)` | connection trỏ merchant tenant khác bị từ chối |
| O4 | Binding connection ↔ tài khoản cùng merchant, cùng environment | `BindConnectionAccount` kiểm merchant/environment hai phía | `pm_connection_account_bindings PK(connection_id, receiving_account_id)`; `FK(tenant_id, merchant_id, environment, connection_id)`; `FK(tenant_id, merchant_id, environment, receiving_account_id)` | bind tài khoản merchant B vào connection merchant A cùng tenant bị từ chối; bind tài khoản Live vào connection Test bị từ chối |
| O5 | Intent thuộc merchant, tài khoản của chính merchant, environment của connection | `CreateIntent` tìm connection `active` bind với tài khoản; environment lấy từ connection | `pm_payment_intents FK(tenant_id, merchant_id, environment, receiving_account_id)`; `UQ(tenant_id, idempotency_key, environment)`; `UQ(payment_reference)`; `CHECK(amount_vnd > 0)` | intent merchant A dùng tài khoản merchant B cùng tenant bị từ chối; intent Test dùng tài khoản Live bị từ chối; `amount_vnd = 0` bị từ chối |
| O6 | Intent ↔ profile/prefix có tên | `CreateIntent` nhận tên prefix; tên lạ → `UnknownReferencePrefix` | `FK(reference_profile_version)`; `FK(reference_profile_version, reference_prefix_name) → pm_reference_profile_prefixes` khi non-legacy | intent trỏ `(version, prefix_name)` không tồn tại bị từ chối |
| O7 | Supersession cùng scope | Chỉ host đổi số tiền mới tạo intent thay thế | self-FK `(tenant_id, superseded_by_intent_id)` | supersession chéo tenant bị từ chối |
| O8 | Readiness theo connection × version × environment | `RecordConnectionReadiness` kiểm checklist đủ mục cho mọi prefix có tên | `UQ(connection_id, profile_version, environment)`; `FK(tenant_id, connection_id)`; `FK(profile_version)` | readiness trùng khoá bị từ chối; readiness trỏ connection tenant khác bị từ chối |
| O9 | Inbox thuộc connection | Tenant/merchant lấy từ connection sau HMAC | `pm_webhook_inbox UQ(connection_id, event_key)`; `FK(tenant_id, connection_id)`; `body_sha256 NOT NULL` | inbox trùng `event_key` cùng connection bị từ chối; inbox tenant sai với connection bị từ chối |
| O10 | Observation ↔ connection/inbox/run cùng scope | Adapter tính `reported_account_key` từ payload đã verify | `UQ(provider, source, source_tx_id, connection_id)`; `CHECK((source='webhook' AND inbox_id IS NOT NULL) OR (source='api' AND reconciliation_run_id IS NOT NULL))`; composite FK cùng tenant tới connection, inbox, run | observation trỏ inbox/run của connection hoặc tenant khác bị từ chối; observation webhook thiếu `inbox_id` bị từ chối |
| O11 | Observation ↔ fact canonical cùng scope | Link-or-create chạy dưới khoá scope, đọc lại trước khi tạo | composite FK `(tenant_id, environment, provider, reported_account_key, transaction_id)` tới fact khi `transaction_id` khác NULL | observation Test link fact Live, hoặc fact tenant khác, bị từ chối |
| O12 | Fact canonical: identity và receiver | Guard: chiều tiền trước, rồi receiver thuộc binding cùng merchant | `UQ(dedup_key)`; partial `UQ(tenant_id, environment, provider, provider_account_key, webhook_tx_id)` và tương tự `api_tx_id`; `FK(tenant_id, merchant_id, environment, receiving_account_id)` khi đã resolve; `CHECK(match_state <> 'settled' OR (receiving_account_id IS NOT NULL AND direction = 'in'))`; `amount_vnd NOT NULL`, `CHECK(amount_vnd >= 0)`; `bank_reference` chỉ index thường | hai fact cùng `webhook_tx_id` trong scope bị từ chối; fact `settled` với `direction='out'` hoặc receiver NULL bị từ chối; fact resolve tới tài khoản merchant khác bị từ chối; hai fact cùng `bank_reference` được nhận |
| O13 | Duplicate-of cùng scope | `mark_duplicate_of` cần provenance có cấu trúc | self-FK `(tenant_id, environment, duplicate_of_transaction_id)` | duplicate-of chéo tenant hoặc environment bị từ chối |
| O14 | Settlement đúng tiền, đúng receiver, đúng scope | Lõi post-check `tx.amount == intent.amount`; khoá intent `FOR UPDATE` | `UQ(transaction_id)`; `UQ(intent_id)`; `CHECK(amount_vnd = intent_amount_vnd)`, cả hai `NOT NULL`; `FK(tenant_id, environment, transaction_id, amount_vnd, receiving_account_id) → fact`; `FK(tenant_id, environment, intent_id, intent_amount_vnd, receiving_account_id) → intent` | settlement lệch tiền với `origin='auto'` **và** `origin='operator_review'` bị từ chối; settlement receiver khác intent bị từ chối; settlement cho fact receiver NULL bị từ chối; hai settlement cùng intent hoặc cùng fact bị từ chối; fact Test settle intent Live bị từ chối; `amount_vnd` NULL bị từ chối |
| O15 | Settlement operator có provenance review | `ResolveReview` nhận actor, reason; host kiểm quyền trước | `CHECK(origin = 'auto' OR (review_case_id IS NOT NULL AND resolved_by IS NOT NULL))`; `FK(review_case_id, tenant_id, transaction_id) → pm_review_cases` | settlement operator thiếu review case, hoặc case thuộc fact khác, bị từ chối |
| O16 | Review case cùng tenant, một case mở mỗi fact | `TENANT_MISMATCH` mở với `candidate_intent_id = NULL` | partial `UQ(transaction_id) WHERE status='open'`; `FK(tenant_id, transaction_id)`; `FK(tenant_id, candidate_intent_id)` | hai review `open` cùng fact bị từ chối; `candidate_intent_id` chéo tenant bị từ chối |
| O17 | Profile: đúng một active; prefix hợp lệ | `check_prefix_overlap`: trùng/lồng nhau trong version → lỗi; giữa version → advisory | partial `UQ(status) WHERE status='active'`; `CHECK(kind='legacy_import' OR (suffix_length BETWEEN 1 AND 30 AND alphabet IS NOT NULL))`; `pm_reference_profile_prefixes PK(profile_version, name)`, `UQ(profile_version, prefix)`, `CHECK(length(prefix) BETWEEN 2 AND 5)` | hai profile `active` bị từ chối; hai prefix cùng giá trị trong một version bị từ chối; prefix 1 hoặc 6 ký tự bị từ chối; cùng prefix ở hai version được nhận |
| O18 | Đối soát tự settle cần bằng chứng | `SetReconcileMode` chỉ nhận evidence hợp lệ | `CHECK(reconcile_mode = 'detect_only' OR reconcile_evidence_ref IS NOT NULL)`; `CHECK(timestamp_tolerance_seconds BETWEEN 60 AND 7200)`, mặc định 300 | `auto_settle` thiếu `reconcile_evidence_ref` bị từ chối; tolerance 30 bị từ chối |

Luật "không lồng nhau trong một version" (`SUB`/`SUBX`) không biểu diễn được bằng constraint di động nên chỉ kiểm ở application (unit test domain). Scope dedup/identity là `tenant + environment + provider + provider_account_key`; `bank_reference` không bao giờ unique.

## Ma trận trạng thái và ranh giới transaction

Mỗi chuyển trạng thái nêu use case kích hoạt, transaction chứa nó và phase có test tương ứng. "Chờ SePay Test" nghĩa là quy tắc phụ thuộc payload thật; kịch bản SePay Test đang hoãn (chưa có credential), nên giữ mặc định an toàn `detect_only`, 300 giây và không suy diễn identity/thời gian từ payload.

| Đối tượng | Chuyển trạng thái | Use case / điều kiện | Ranh giới transaction | Test |
|---|---|---|---|---|
| ReferenceProfile | `draft → active` (v mới) và `active → accepted_legacy` (v cũ) | `ActivateReferenceProfile`: mọi connection `active` có readiness `ready` cho v mới và cho prefix của mọi version cũ còn accepted | **Một** transaction cho cả hai cập nhật; partial unique bảo đảm không bao giờ 0 hoặc 2 active sau commit | Phase review/profile: rotation nguyên tử, không-active/hai-active |
| ReferenceProfile | `accepted_legacy → retired` | `RetireReferenceProfile` có thời hạn nghiệp vụ; không tự retire khi intent hết hạn | Transaction riêng | Phase review/profile |
| ProviderConnection | `pending → active` | `RecordConnectionReadiness(ready)` cho profile active | Cùng transaction với ghi readiness | Phase review/onboarding |
| ProviderConnection | `active → not_ready` | `SetConnectionStatus` có chủ đích, hoặc binding/environment/credential/tài khoản/prefix/bộ lọc đổi sau readiness (vô hiệu readiness, `reconcile_mode` về `detect_only`) | Cùng transaction với thay đổi gây vô hiệu | Phase review/onboarding: readiness stale |
| ProviderConnection | `* → disabled`, `not_ready → active` | `SetConnectionStatus`; quay lại `active` phải qua readiness đầy đủ | Transaction riêng, có audit | Phase review/onboarding: mọi đường trở lại `active` |
| WebhookInbox | tạo `received` / `quarantined` | `IngestWebhook` sau HMAC; thiếu id → `quarantined`, khoá `sha256(raw_body)` | Transaction tiếp nhận riêng, commit rồi mới ACK | Phase ingest/processing |
| WebhookInbox | `received/retry_wait → processing` | Claim: `FOR UPDATE SKIP LOCKED`, `lease_generation + 1`, `lease_until` | Transaction claim riêng, commit ngay | Phase storage: claim song song, reclaim |
| WebhookInbox | `processing → processed` | Ghi observation + fact + quyết định (+ settlement/outbox) | Transaction xử lý; finalize là CAS theo `lease_generation` | Phase ingest/processing |
| WebhookInbox | `processing → retry_wait / failed / quarantined` | Lỗi tạm / vượt số lần / normalize thất bại | Transaction ghi lỗi riêng sau rollback xử lý; CAS theo generation | Phase ingest/processing: rollback handler để lại việc retry được |
| WebhookInbox | lease hết hạn → claim lại | Claim thấy `processing AND lease_until < now` | Transaction claim mới, generation mới; owner cũ finalize thì CAS thất bại | Phase storage: owner cũ không ghi sau reclaim |
| WebhookInbox | `failed/quarantined → received` | `RequeueInbox` của operator | Transaction riêng, có audit | Phase ingest/processing |
| OutboxEvent | `pending → published / failed`, reclaim | `DispatchOutbox` claim + publish at-least-once | Claim, publish, ghi kết quả tách biệt; CAS generation | Phase storage + host mẫu |
| Observation ↔ fact | link-or-create | Webhook hoặc Reconcile: khoá scope (hoặc SERIALIZABLE có retry giới hạn), đọc lại, rồi link hoặc tạo | Cùng transaction xử lý của nguồn đó | Phase adapter/reconcile: webhook + API đồng thời → một canonical |
| Observation API | `unlinked → linked / ambiguous`, hoặc tạo canonical sau grace | Quét observation `unlinked` quá grace, độc lập cursor | Transaction riêng mỗi observation hoặc lô nhỏ | Phase adapter/reconcile: cursor đã tiến vẫn xét lại |
| Luật link `bank_reference` và `webhook_success` | — | Chờ SePay Test; tới khi có bằng chứng giữ `detect_only` | — | Phase evidence gate |
| ProviderTransaction | `recorded → settled / in_review / not_applicable` | `MatchTransaction` | Cùng transaction xử lý; settlement + intent `paid` + outbox nguyên tử | Phase domain (unit) + ingest/processing |
| ProviderTransaction | `in_review → settled / closed_external / duplicate_of` | `ResolveReview`, `RematchUnbound` | Transaction riêng, khoá intent `FOR UPDATE`, đóng review cùng commit | Phase review |
| PaymentIntent | `awaiting_payment → paid / expired / cancelled / superseded`; `expired → paid` | Settlement; hết hạn; host huỷ; host đổi số tiền; operator `accept_late` đúng số tiền | Cùng transaction với settlement hoặc lệnh host | Phase domain + review |
| Late webhook | `inbox.received_at > intent.expires_at` | Đồng hồ server | — | Phase domain |
| Late API | thời gian provider chỉ dùng khi đã xác minh; nếu không, không auto-settle intent quá hạn | Chờ SePay Test cho định dạng thời gian | — | Phase domain + adapter |

## Giới hạn còn lại

- Tên trường tài khoản trong payload thật (`accountNumber`/`subAccount`, `account_number`/`va`) và cầu nối `bank_reference` chưa xác minh trên SePay Test; schema không phụ thuộc kết quả, chỉ quyết định lúc bật `auto_settle`.
- Các trường `*_masked` chỉ dùng hiển thị; không dùng masked data để tạo QR, fingerprint hay `provider_account_key`.
- `REFERENCE_PROFILE` và `REFERENCE_PROFILE_PREFIX` là cấu hình cấp project, không mang `tenant_id`. Readiness kế thừa scope connection.
- Reference unique toàn project chỉ trong một database. Nhiều bản cài độc lập dùng chung tài khoản nhận phải chọn prefix rời nhau; DB cục bộ không bảo đảm unique giữa các bản cài.

Xem [câu hỏi mở](decisions-and-open-questions.md).
