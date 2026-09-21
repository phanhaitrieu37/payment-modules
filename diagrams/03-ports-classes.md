# D03 — Ports và lớp OOP

[← Mục lục](../readme.md) · [Danh mục sơ đồ](../diagrams.md)

Trạng thái: sơ đồ kiến trúc đề xuất; không phải bằng chứng triển khai. Nguồn Mermaid được chuyển nguyên từ bản thiết kế đã render/kiểm tra ngày 21/09/2026.

Interface nhỏ (Protocol) với implementation thay thế được. PaymentService ghép các port bằng composition, không dùng chuỗi kế thừa theo dự án/merchant.

- InvariantGuard thuộc lõi và luôn chạy trước MatchingPolicy: receiver phải thuộc connection, tiền phải là tiền vào, tenant phải khớp. Policy tuỳ biến không thể nới các điều kiện này.
- TransactionReader là capability riêng: provider không có API đối soát vẫn dùng được phần còn lại.
- MatchingPolicy chỉ quyết định khớp hay review; không bỏ qua được bước xác thực hay chống trùng.
- PaymentReferenceGenerator sinh mã từ ReferenceProfile bất biến; RandomSuffixGenerator là strategy mặc định. ReferenceTemplateChecklist (adapter SePay) chỉ kiểm tra và liệt kê việc cần làm, không tự cấu hình SePay.
- REVIEW_AMBIGUOUS_REFERENCE: không có hoặc có nhiều ứng viên mâu thuẫn thì vào review, không đoán.
- SettlementHandler do host implement; lõi không biết gói, bill hay quyền lợi của host.

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
    +verify(raw_body, headers, secrets) VerifiedDelivery
    +normalize(delivery) NormalizedTransaction
    +build_instruction(intent) TransferInstruction
  }
  class TransactionReader {
    <<Protocol capability>>
    +list_transactions(connection, window, cursor) Page
  }
  class ConnectionResolver {
    <<Protocol>>
    +by_locator(locator) ProviderConnection
  }
  class SecretResolver {
    <<Protocol>>
    +active_secrets(connection) list~Secret~
  }
  class MatchingPolicy {
    <<Protocol>>
    +decide(tx, candidate_intent) MatchDecision
  }
  class ExactAmountPolicy {
    +decide(tx, candidate_intent) MatchDecision
  }
  class SettlementHandler {
    <<Protocol - host>>
    +on_settled(uow, settlement) None
  }
  class UnitOfWork {
    <<Protocol>>
    +intents IntentRepository
    +transactions TransactionRepository
    +settlements SettlementRepository
    +inbox InboxRepository
    +outbox OutboxRepository
    +commit() None
  }
  class SePayProvider {
    +verify() HMAC-SHA256 timestamp.raw_body
    +normalize() NormalizedTransaction
  }
  class SePayTransactionReader {
    +list_transactions() Page
  }
  class SqlAlchemyUnitOfWork {
    +commit() None
  }
  class PaymentService {
    -provider_registry
    -uow_factory
    -policy MatchingPolicy
    -handler SettlementHandler
    +create_intent(cmd) PaymentIntent
    +ingest(locator, raw_body, headers) Ack
    +process_inbox(batch) None
    +reconcile(connection, window) RunResult
  }
  class PaymentReferenceGenerator {
    <<Protocol>>
    +generate(profile) PaymentReference
  }
  class RandomSuffixGenerator {
    +generate(profile) PaymentReference
    hậu tố ngẫu nhiên, retry khi unique DB trùng
  }
  class ReferenceProfile {
    <<value - bất biến>>
    +version int
    +prefix str
    +suffix_length int
    +alphabet str
  }
  class ReferenceTemplateChecklist {
    <<Protocol - provider>>
    +validate(profile) list~Issue~
    +checklist(connection, profile) Checklist
  }
  class SePayTemplateChecklist {
    +validate() tiền tố 2-5, hậu tố 1-30
    +checklist() mẫu công ty, bộ lọc webhook
  }
  class InvariantGuard {
    <<core - không thay thế được>>
    +check(tx, connection) GuardResult
    receiver thuộc connection
    chiều tiền = vào
    tenant khớp
  }
  class MatchDecision {
    <<enumeration>>
    SETTLE
    REVIEW_AMOUNT_MISMATCH
    REVIEW_LATE
    REVIEW_NO_REFERENCE
    REVIEW_UNVERIFIED_IDENTITY
    REVIEW_AMBIGUOUS_REFERENCE
  }
  PaymentProvider <|.. SePayProvider
  TransactionReader <|.. SePayTransactionReader
  MatchingPolicy <|.. ExactAmountPolicy
  UnitOfWork <|.. SqlAlchemyUnitOfWork
  PaymentService o-- PaymentProvider
  PaymentService o-- TransactionReader
  PaymentService o-- UnitOfWork
  PaymentService o-- MatchingPolicy
  PaymentService o-- SettlementHandler
  PaymentService o-- ConnectionResolver
  PaymentService o-- SecretResolver
  PaymentService o-- PaymentReferenceGenerator
  PaymentReferenceGenerator <|.. RandomSuffixGenerator
  PaymentReferenceGenerator ..> ReferenceProfile
  ReferenceTemplateChecklist <|.. SePayTemplateChecklist
  ReferenceTemplateChecklist ..> ReferenceProfile
  MatchingPolicy ..> MatchDecision
  PaymentService *-- InvariantGuard : chạy TRƯỚC policy
```
