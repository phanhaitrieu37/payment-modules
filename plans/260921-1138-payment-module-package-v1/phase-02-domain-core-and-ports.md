# Phase 02 — Domain lõi, chuỗi quyết định và ports

## Context Links

- [plan.md](plan.md) · [Phase 01](phase-01-design-reconciliation-and-repo-bootstrap.md)
- Kongming §4 (use case/port), §5 (Guard/Eligibility/Policy), §7 (settlement) — `/Users/trieuphan/source_code/payment_modules/payment_modules_v1/plans/reports/kongming-260921-1734-payment-design-fix-options.md`
- `/Users/trieuphan/source_code/payment_modules/payment_modules_v1/diagrams/03-ports-classes.md`, `09-state-payment-intent.md`, `10-state-inbox-transaction.md`
- > **Tham chiếu hành vi (chỉ đọc, không sao chép — quyết định N1 21/09/2026).** MeowAI (`git -C /Users/trieuphan/source_code/stk-meowai/MeowAI show origin/main:<path>`) dùng để đối chiếu *hành vi và biên ca*, không phải nguồn code. Mọi code, hằng số, tên hàm, docstring, bảng dữ liệu test trong package được viết từ tài liệu thiết kế + tài liệu SePay/NAPAS/EMVCo. Không copy-paste, không "port từng hàm", không import, không ghi output của MeowAI làm hằng số test nếu chưa được kiểm chéo bằng nguồn độc lập. Lý do: package phát hành độc lập, ranh giới sở hữu rõ (MeowAI là repo riêng: gốc DeerFlow MIT + code riêng của chủ dự án); reviewer từ chối PR có đoạn giống nguyên văn.
- Đọc để đối chiếu biên ca (không chép): MeowAI `backend/packages/harness/deerflow/entitlements/orders_repo.py` (tách token memo), `backend/packages/harness/deerflow/entitlements/codes.py` (alphabet không nhập nhằng).

## Overview

- Priority: P0
- Status: done (22/09/2026)
- Mô tả: viết phần lõi thuần Python (không ORM, không FastAPI, không httpx): value objects, enums, state transitions, domain service `MatchTransaction` với chuỗi lõi không thay được, và các Protocol port. 100% unit test không cần DB.

## Key Insights

- Policy của host chỉ được **thắt chặt**, không nới: lõi kiểm lại đầu ra `SETTLE` (đúng số tiền, receiver bind, chiều `in`, intent eligible). Với U9 không còn `accepted_variance` — đường nới duy nhất bị xoá khỏi thiết kế.
- `IntentEligibility` chặn policy thấy intent `paid/cancelled/superseded`; `expired` (hoặc `awaiting_payment` mà `received_at > expires_at`) đi tiếp với `is_late=True`.
- `TENANT_MISMATCH` (quyết định người dùng D6): `payment_reference` unique toàn project nên `ReferenceResolver` có thể trả intent của tenant khác với tenant của receiver đã bind. Composite FK chặn settlement chéo tenant, nhưng sự kiện này vẫn là tín hiệu nghiêm trọng → **alert + mở review + metric** (không còn inbox `failed`). Không lý do nào trong 9 lý do cũ đúng ngữ nghĩa (`RECEIVER_UNBOUND` = receiver chưa bind; `NO_REFERENCE` = không có mã) nên thêm `TENANT_MISMATCH` làm `ReviewReason` thứ 10. Review mở trong tenant của transaction với `candidate_intent_id = NULL` (FK tenant không cho trỏ intent tenant khác; `details` không chứa id/tenant của intent bên kia).
- Prefix (quyết định người dùng D1): một version profile có **nhiều prefix có tên** dùng chung `suffix_length` và alphabet. Prefix không phải xác thực (bất biến 11) nên không đổi chuỗi khớp; nó chỉ quyết định mã sinh ra và checklist SePay. Kiểm chồng lấn (trùng hoặc prefix này là đầu của prefix kia) là hàm thuần của `ReferenceResolver`, chạy lúc tạo profile: **trong một version** → lỗi `PrefixOverlap`; **giữa các version** còn được chấp nhận (`active`, `accepted_legacy`, `kind=generated`) → chỉ advisory (không raise), vì `UQ(payment_reference)` toàn project + khớp nguyên token đã bảo đảm một chuỗi mã chỉ thuộc một intent/một version. Advisory được checklist SePay dùng để tính mẫu suffix min/max (phase 07). Ví dụ alphabet mặc định: 32 ký tự không nhập nhằng (cấu hình của profile, không phải hằng số cứng).
- Khớp nguyên token: tách bằng `[^A-Za-z0-9_-]+`, upper-case (luật của plan, là normalization contract có version); nhiều intent khác nhau từ `code`/`content` → `AMBIGUOUS_REFERENCE`.

## Requirements

Functional:
- `AmountVnd` (luật: `bool` → từ chối; `int` ≥ 0; `float` chỉ khi `.is_integer()`; chuỗi toàn chữ số → int; khác → không hợp lệ), `Direction {IN, OUT, UNKNOWN}`.
- `NamedPrefix(name, prefix)`; `ReferenceProfile(version, prefixes: tuple[NamedPrefix, ...], suffix_length, alphabet, kind, status)`; `kind ∈ {generated, legacy_import}`; validate `generated`: ≥1 prefix; `name` `^[a-z][a-z0-9_]{0,31}$` và không trùng; mỗi prefix `^[A-Z]{2,5}$` (giới hạn SePay; >5 bị từ chối); không có hai prefix trùng hoặc lồng nhau; `1 ≤ suffix_length ≤ 30`, alphabet ⊆ `A–Z0–9` và là `0–9` hoặc tập con chữ+số (chung cho mọi prefix); `legacy_import` không có prefix, không sinh mã, chỉ được `accepted_legacy`. `profile.prefix_for(name)` → prefix; tên lạ → `UnknownReferencePrefix`.
- `ReferenceResolver.check_prefix_overlap(candidate: Sequence[NamedPrefix], accepted: Sequence[PrefixShape]) -> PrefixOverlapReport{errors: list[PrefixOverlap], advisories: list[CrossVersionOverlap]}` (thuần) — `errors` chỉ cho trùng/lồng nhau (`a.startswith(b)` hoặc ngược lại) **trong** `candidate`; `advisories` cho cặp candidate × accepted cùng prefix hoặc lồng nhau, mang `(prefix, suffix_length, alphabet)` hai bên để checklist tính min/max. `PrefixShape = (prefix, suffix_length, alphabet, version)`. `ReferenceProfile` validate và `CreateReferenceProfile` (phase 06) dùng chung hàm này.
- `account_key(bank_code, account_number, sub_account) -> str` (`domain/account_identity.py`): chuỗi chuẩn hoá `BANK|ACCOUNT|SUB` (strip, upper, bỏ khoảng trắng, sub rỗng = chuỗi rỗng) — dùng chung cho `provider_account_key` (adapter tính từ payload) và `account_fingerprint` (onboarding) để guard so bằng đẳng thức.
- `PaymentReference.normalize()` (strip + upper); `tokens_from(code, content) -> list[str]`.
- Enums: `IntentStatus {awaiting_payment, paid, expired, cancelled, superseded}`, `MatchState {recorded, settled, in_review, not_applicable, closed_external, duplicate_of}`, `InboxStatus {received, processing, processed, retry_wait, failed, quarantined}`, `ReviewReason` (đúng 10: `RECEIVER_UNBOUND, NO_REFERENCE, AMBIGUOUS_REFERENCE, TENANT_MISMATCH, ALREADY_PAID, INTENT_CANCELLED, INTENT_SUPERSEDED, AMOUNT_MISMATCH, LATE, UNVERIFIED_IDENTITY`), `ReviewResolution {attach_to_intent, mark_external, mark_duplicate_of, bind_receiver, accept_late}`, `ReconcileMode {detect_only, auto_settle}`, `ConnectionStatus {pending, active, not_ready, disabled}`, `SettlementOrigin {auto, operator_review}`.
- Chuỗi trong `MatchTransaction.decide(ctx) -> MatchOutcome`:
  1. `InvariantGuard.check(tx, bound_account_ids, connection)` → `PASS | OUTGOING | RECEIVER_UNBOUND` (`OUTGOING` gồm cả `UNKNOWN`).
  2. `ReferenceResolver.resolve(tokens, candidates_by_reference)` → 0 → `NO_REFERENCE`; >1 intent → `AMBIGUOUS_REFERENCE`; đúng 1 intent nhưng `intent.tenant_id != tx.tenant_id` → `TENANT_MISMATCH` (review, `candidate_intent_id=None`; application phát alert + metric theo lý do này — phase 05).
  3. `IntentEligibility.check(intent, received_at)` → `ALREADY_PAID | INTENT_CANCELLED | INTENT_SUPERSEDED(candidate=superseded_by)` hoặc tiếp tục với `is_late`.
  4. `MatchingPolicy.decide(tx, intent, is_late) -> MatchDecision` (port; mặc định `ExactAmountPolicy`: late → `LATE`, lệch → `AMOUNT_MISMATCH{direction: under|over}`).
  5. Lõi kiểm lại: `SETTLE` chỉ hợp lệ nếu `tx.amount == intent.amount` và không late; ngược lại raise `PolicyViolation` (bug của policy host ⇒ không bao giờ settle).
- `MatchOutcome` = `Settle(intent_id)` | `Review(reason, candidate_intent_id, details)` | `NotApplicable`.
- Guard xét chiều tiền trước receiver và mode đối soát: `OUT`/`UNKNOWN` luôn `NotApplicable`, kể cả receiver chưa bind hoặc nguồn API. Với tiền vào, cùng tenant chưa đủ: merchant của connection, receiving account và intent phải đồng nhất; sai scope không được chuyển cho policy.
- Matching context có `effective_received_at` và nguồn bằng chứng thời gian. Webhook dùng `inbox.received_at`; API chỉ dùng thời gian provider khi phase 03 đã xác minh định dạng/múi giờ, nếu không dùng `observation.observed_at` và review khi không chứng minh được thanh toán đúng hạn. Post-check không cho policy tự nới thời gian hoặc exact amount; `accept_late` của operator ở phase 06 là đường có audit riêng và vẫn buộc đúng số tiền.
- Transition functions (thuần) cho intent và transaction, raise `IllegalTransition` khi sai (vd `settled → in_review`).
- Domain events (dataclass, `schema_version=1`): `PaymentSettled`, `PaymentNeedsReview`, `ReviewResolved`; định nghĩa rõ field, trusted scope và JSON payload canonical để phase 08/09 kiểm contract từ wheel.

Ports (Protocol, `src/payment_module/ports/`):
- `PaymentProvider`: `code`, `verify(raw_body, headers, secrets, now, tolerance_s) -> VerifiedDelivery`, `extract_event_key(verified) -> EventKey | None`, `normalize(verified) -> NormalizedObservation`, `build_instruction(intent, account) -> TransferInstruction`.
- `TransactionReader` (capability): `list_page(connection, credential, cursor, window) -> Page`.
- `ConnectionResolver.by_locator(locator)`, `SecretResolver.webhook_secrets(connection) -> list[str]` (cửa sổ xoay), `SecretResolver.api_credential(connection) -> str | None`.
- `UnitOfWork` (bó repository: merchants, receiving_accounts, connections, connection_bindings, reference_profiles, readiness, intents, inbox, observations, transactions, settlements, review_cases, outbox, reconciliation_runs; `commit()`, `rollback()`).
- `PaymentReferenceGenerator.generate(profile, prefix_name) -> PaymentReference` (= `profile.prefix_for(prefix_name)` + suffix ngẫu nhiên; tên lạ → `UnknownReferencePrefix`).
- `ReferenceTemplateChecklist.validate(profile) -> list[Issue]`, `.checklist(connection, profile) -> Checklist` (một mục mẫu + một mục bộ lọc webhook **cho mỗi prefix có tên**, cộng các mục chung).
- `SettlementHandler.on_settled(uow, settled: SettlementView) -> None` (cùng UoW, không commit, không gọi mạng).
- `OutcomeObserver.on_outcome(uow, outcome: TransactionOutcomeView) -> None` (cùng UoW, chỉ nhận DTO bất biến; mặc định no-op) — dùng cho projection đồng bộ của host.
- `OutboxPublisher.publish(event: OutboxEventView) -> None` (async, cho phương án B).
- `Clock.now()`.

Non-functional: domain + ports không import `sqlalchemy`, `fastapi`, `httpx` (test kiểm bằng AST/import-linter đơn giản).

## Architecture

```
application (phase 05/06/07) ──> domain.matching.MatchTransaction
                                   ├─ InvariantGuard      (lõi)
                                   ├─ ReferenceResolver   (lõi)
                                   ├─ IntentEligibility   (lõi)
                                   ├─ MatchingPolicy      (port; ExactAmountPolicy mặc định)
                                   └─ post-check          (lõi)
```

`MatchTransaction` nhận dữ liệu đã nạp (tx, bound accounts, candidate intents, received_at) — không tự truy vấn; application nạp dữ liệu và khoá `FOR UPDATE` trước khi gọi. Giữ domain thuần, test nhanh.

## Related Code Files

Create (tất cả dưới `/Users/trieuphan/source_code/payment_modules/payment_modules_v1/`):
- `src/payment_module/domain/account_identity.py`
- `src/payment_module/domain/money.py`, `enums.py`, `reference.py`, `intent.py`, `transaction.py`, `review.py`, `events.py`, `errors.py`
- `src/payment_module/domain/matching/invariant_guard.py`, `reference_resolver.py`, `intent_eligibility.py`, `exact_amount_policy.py`, `match_transaction.py`
- `src/payment_module/ports/provider.py`, `reader.py`, `resolvers.py`, `unit_of_work.py`, `reference_generator.py`, `template_checklist.py`, `handlers.py` (SettlementHandler, OutcomeObserver), `publisher.py`, `clock.py`
- `src/payment_module/reference/random_suffix_generator.py` (`secrets.choice`, retry do tầng application xử lý khi unique DB trùng)
- `tests/unit/domain/test_money.py`, `test_reference_profile.py`, `test_reference_tokens.py`, `test_intent_transitions.py`, `test_transaction_transitions.py`, `test_invariant_guard.py`, `test_reference_resolver.py`, `test_intent_eligibility.py`, `test_exact_amount_policy.py`, `test_match_transaction.py`, `test_layer_imports.py`

Modify/Delete: không.

## Implementation Steps

1. TDD: viết test cho từng value object/enum trước (bảng tham số: amount bool/float/str/âm; prefix 1/2/5/6 ký tự, chữ thường, có số; suffix 0/1/30/31; profile 0 prefix, 3 prefix hợp lệ, tên trùng, prefix trùng, prefix lồng nhau `SUB`/`SUBX`, `prefix_for` tên lạ → `UnknownReferencePrefix`; cùng prefix khác `suffix_length` giữa version → không lỗi, có advisory).
2. `reference.py`: tokenization theo luật ở Key Insights; bảng ca tự soạn: mã có dấu chấm/gạch ngang/gạch dưới bao quanh, chữ thường, nhiều mã, mã liền chuỗi khác (không tách), unicode, memo rỗng/None. Có thể đọc test MeowAI để so độ phủ, không chép dữ liệu.
3. Transitions intent/transaction theo D09/D10 đã sửa ở phase 01; test mọi cặp (from, to) hợp lệ/không hợp lệ.
4. Guard → Resolver → Eligibility → Policy → post-check; test từng lý do trong 10 `ReviewReason` (trừ `UNVERIFIED_IDENTITY` do Reconcile đặt, test ở phase 07; `TENANT_MISMATCH` test mã khớp intent tenant khác → review, `candidate_intent_id=None`) và test "policy host trả SETTLE khi lệch tiền → `PolicyViolation`".
   - Test `out/unknown + receiver chưa bind` → `NotApplicable` và test tenant/merchant/receiver sai không lọt tới policy. Với API-only không có timestamp tin cậy, intent đã quá hạn không thể auto-settle; test riêng đường operator `accept_late` vẫn exact amount.
5. Ports dạng `typing.Protocol` + DTO `@dataclass(frozen=True, slots=True)`.
6. `test_layer_imports.py`: duyệt `src/payment_module/domain` và `ports`, fail nếu import `sqlalchemy|fastapi|httpx|starlette`.
7. `uv run pytest tests/unit -q`, `uv run ruff check .`; commit `feat(domain): add payment domain model and core matching chain`.

## Todo List

- [x] Value objects + validate profile nhiều prefix có tên + `check_prefix_overlap`
- [x] Enums (đúng 10 ReviewReason, gồm `TENANT_MISMATCH`)
- [x] Tokenization nguyên token + bảng ca
- [x] Transitions + test bảng
- [x] Chuỗi Guard/Resolver/Eligibility/Policy/post-check
- [x] Scope merchant và effective receipt time; event payload/DTO v1 có contract test
- [x] Ports Protocol + DTO
- [x] Test chặn import tầng ngoài

## Success Criteria

- `cd /Users/trieuphan/source_code/payment_modules/payment_modules_v1 && uv run pytest tests/unit -q` → pass, không cần Docker.
- `uv run python -c "from payment_module.domain.enums import ReviewReason as R; assert len(R) == 10 and R.TENANT_MISMATCH"` → exit 0.
- `uv run pytest tests/unit/domain/test_match_transaction.py -q -k "policy_cannot_widen"` → pass.
- Unit tests cho tiền ra/unknown ở cả nguồn, merchant khác cùng tenant, và API-only sau expiry đều không auto-settle; snapshot event JSON giữ `schema_version=1` và trusted scope.
- `uv run pytest tests/unit/domain/test_layer_imports.py -q` → pass.
- `uv run pytest tests/unit/domain/test_reference_profile.py -q -k "nested_prefix_rejected or unknown_prefix_name or same_prefix_across_versions_allowed"` → pass.

## Risk Assessment

- Host có mã cũ không theo profile → `kind=legacy_import` (không validate sinh mã).
- Advisory cross-version bị bỏ qua → mẫu SePay thiếu min/max: checklist phase 07 tự tính từ advisory, readiness phase 06 bắt buộc tích mục đó.

## Security Considerations

- Prefix không phải xác thực (bất biến 11): không có code path nào dùng `startswith(prefix)` để quyết định settle; test khẳng định mã khác intent dù cùng prefix không khớp.
- Không log memo/tên người chuyển trong domain (domain không log).

## Next Steps

- Phase 04 hiện thực `UnitOfWork`; phase 05 dùng `MatchTransaction`.
