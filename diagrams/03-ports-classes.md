# D03 — Ports và lớp OOP

[← Mục lục](../readme.md) · [Danh mục sơ đồ](../diagrams.md)

Trạng thái: sơ đồ kiến trúc đã đối chiếu với các delta đã duyệt (22/09/2026); không phải bằng chứng triển khai. Mermaid đã sửa sau lần render 21/09/2026 và chưa render lại.

Interface nhỏ (Protocol) với implementation thay thế được. Không có lớp `PaymentService` gom mọi port: mỗi use case nhận đúng port nó dùng, `build_payment_module` chỉ lắp ghép (container dataclass, không logic). Không dùng chuỗi kế thừa theo dự án/merchant.

- `MatchTransaction` là domain service dùng chung cho ProcessInbox, Reconcile và ResolveReview/RematchUnbound. Chuỗi lõi không thay được: `InvariantGuard` (chiều tiền trước, rồi receiver/merchant) → `ReferenceResolver` (khớp nguyên token) → `IntentEligibility` (policy không bao giờ thấy intent `paid/cancelled/superseded`) → `MatchingPolicy` (port duy nhất host thay được) → post-check của lõi.
- Policy chỉ được **thắt chặt**: post-check từ chối `SETTLE` khi số tiền khác intent hoặc late (`PolicyViolation`). U9: không có `accepted_variance`, không có đường operator settle lệch tiền.
- `MatchDecision` là dataclass `{outcome: SETTLE | REVIEW, reason: ReviewReason | None, details}`. `ReviewReason` có đúng **10** giá trị; `TENANT_MISMATCH` (D6) là lý do thứ 10 — alert + mở review + metric, `candidate_intent_id = NULL`. `UNVERIFIED_IDENTITY` do Reconcile đặt, không phải policy.
- `ReferenceProfile` có nhiều `NamedPrefix` (D1) dùng chung `suffix_length`/`alphabet`; `CreateIntent` nhận **tên prefix**, tên lạ → `UnknownReferencePrefix`. Kiểm trùng/lồng nhau là lỗi trong một version, chỉ advisory giữa các version.
- `PaymentProvider.extract_event_key` chỉ đọc `id` sau HMAC; thiếu id → inbox `quarantined` với khoá `sha256(raw_body)`.
- TransactionReader là capability riêng: provider không có API đối soát vẫn dùng được phần còn lại. SettlementHandler/OutcomeObserver do host implement, chạy cùng UoW; OutboxPublisher cho phương án B.

```mermaid
---
config:
  theme: base
  class:
    htmlLabels: false
  fontFamily: Arial, Helvetica, sans-serif
  themeVariables:
    fontFamily: "Arial, Helvetica, sans-serif"
    fontSize: 15px
---
classDiagram
  direction LR
  class PaymentProvider {
    <<Protocol>>
    +code() str
    +verify(raw_body, headers, secrets, now, tolerance_s) VerifiedDelivery
    +extract_event_key(verified) EventKey | None
    +normalize(verified) NormalizedObservation
    +build_instruction(intent, account) TransferInstruction
  }
  class TransactionReader {
    <<Protocol capability>>
    +list_page(connection, credential, cursor, window) Page
  }
  class ConnectionResolver {
    <<Protocol>>
    +by_locator(locator) ProviderConnection
  }
  class SecretResolver {
    <<Protocol>>
    +webhook_secrets(connection) list~str~
    +api_credential(connection) str | None
  }
  class MatchingPolicy {
    <<Protocol>>
    +decide(tx, intent, is_late) MatchDecision
  }
  class ExactAmountPolicy {
    +decide(tx, intent, is_late) MatchDecision
  }
  class SettlementHandler {
    <<Protocol - host>>
    +on_settled(uow, settled) None
  }
  class OutcomeObserver {
    <<Protocol - host>>
    +on_outcome(uow, outcome) None
  }
  class OutboxPublisher {
    <<Protocol - host>>
    +publish(event) None
  }
  class UnitOfWork {
    <<Protocol>>
    +merchants, receiving_accounts, connections
    +connection_bindings, reference_profiles, readiness
    +intents, inbox, observations, transactions
    +settlements, review_cases, outbox, reconciliation_runs
    +commit() None
    +rollback() None
  }
  class SePayProvider {
    +verify() HMAC-SHA256 timestamp.raw_body
    +extract_event_key() id sau HMAC
    +normalize() NormalizedObservation
  }
  class SePayTransactionReader {
    +list_page() Page
  }
  class SqlAlchemyUnitOfWork {
    +commit() None
  }
  class PaymentModule {
    <<container - không logic>>
    +create_intent CreateIntent
    +ingest_webhook IngestWebhook
    +process_inbox ProcessInbox
    +reconcile Reconcile
    +resolve_review ResolveReview
    +rematch_unbound RematchUnbound
    +dispatch_outbox DispatchOutbox
  }
  class CreateIntent {
    <<use case>>
    +execute(cmd with prefix_name) PaymentIntent
  }
  class ProcessInbox {
    <<use case>>
    +run(batch) None
  }
  class Reconcile {
    <<use case>>
    +run(connection) RunResult
  }
  class ResolveReview {
    <<use case>>
    +execute(case, resolution, actor, reason) None
  }
  class MatchTransaction {
    <<domain service - lõi>>
    +decide(ctx) MatchOutcome
  }
  class InvariantGuard {
    <<core - không thay thế được>>
    +check(tx, bound_accounts, connection) GuardResult
    chiều tiền trước: OUT/UNKNOWN → NotApplicable
    receiver + merchant thuộc connection
  }
  class ReferenceResolver {
    <<core>>
    +resolve(tokens, candidates) Resolution
    +check_prefix_overlap(candidate, accepted) PrefixOverlapReport
  }
  class IntentEligibility {
    <<core>>
    +check(intent, effective_received_at) Eligibility
  }
  class PaymentReferenceGenerator {
    <<Protocol>>
    +generate(profile, prefix_name) PaymentReference
  }
  class RandomSuffixGenerator {
    +generate(profile, prefix_name) PaymentReference
    hậu tố ngẫu nhiên, retry khi unique DB trùng
  }
  class ReferenceProfile {
    <<value - bất biến>>
    +version int
    +kind generated | legacy_import
    +prefixes tuple~NamedPrefix~
    +suffix_length int
    +alphabet str
    +prefix_for(name) str
  }
  class NamedPrefix {
    <<value>>
    +name str
    +prefix str A-Z 2-5
  }
  class ReferenceTemplateChecklist {
    <<Protocol - provider>>
    +validate(profile) list~Issue~
    +checklist(connection, profile) Checklist
  }
  class SePayTemplateChecklist {
    +validate() tiền tố 2-5, hậu tố 1-30
    +checklist() mẫu + bộ lọc cho mỗi prefix có tên
  }
  class MatchDecision {
    <<dataclass>>
    +outcome SETTLE | REVIEW
    +reason ReviewReason | None
    +details
  }
  class ReviewReason {
    <<enumeration - 10 giá trị>>
    RECEIVER_UNBOUND
    NO_REFERENCE
    AMBIGUOUS_REFERENCE
    TENANT_MISMATCH
    ALREADY_PAID
    INTENT_CANCELLED
    INTENT_SUPERSEDED
    AMOUNT_MISMATCH
    LATE
    UNVERIFIED_IDENTITY
  }
  class ReviewResolution {
    <<enumeration>>
    attach_to_intent
    mark_external
    mark_duplicate_of
    bind_receiver
    accept_late
  }
  PaymentProvider <|.. SePayProvider
  TransactionReader <|.. SePayTransactionReader
  MatchingPolicy <|.. ExactAmountPolicy
  UnitOfWork <|.. SqlAlchemyUnitOfWork
  PaymentModule o-- CreateIntent
  PaymentModule o-- ProcessInbox
  PaymentModule o-- Reconcile
  PaymentModule o-- ResolveReview
  CreateIntent ..> PaymentReferenceGenerator
  CreateIntent ..> PaymentProvider
  ProcessInbox ..> PaymentProvider
  ProcessInbox ..> SettlementHandler
  ProcessInbox ..> OutcomeObserver
  Reconcile ..> TransactionReader
  Reconcile ..> SecretResolver
  ProcessInbox ..> MatchTransaction
  Reconcile ..> MatchTransaction
  ResolveReview ..> MatchTransaction
  ProcessInbox ..> UnitOfWork
  MatchTransaction *-- InvariantGuard : 1. chạy trước
  MatchTransaction *-- ReferenceResolver : 2.
  MatchTransaction *-- IntentEligibility : 3.
  MatchTransaction o-- MatchingPolicy : 4. port duy nhất thay được
  MatchingPolicy ..> MatchDecision
  MatchDecision ..> ReviewReason
  ResolveReview ..> ReviewResolution
  PaymentReferenceGenerator <|.. RandomSuffixGenerator
  PaymentReferenceGenerator ..> ReferenceProfile
  ReferenceProfile *-- NamedPrefix : 1..*
  ReferenceTemplateChecklist <|.. SePayTemplateChecklist
  ReferenceTemplateChecklist ..> ReferenceProfile
```
