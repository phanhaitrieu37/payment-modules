# Danh mục architecture, sequence và pipeline

[← Mục lục](readme.md)

Các sơ đồ giữ dạng Mermaid v11 trong Markdown để có thể chỉnh sửa. Flowchart dùng ELK theo nguồn gốc; renderer Markdown cần hỗ trợ Mermaid v11 và cấu hình tương ứng. Nếu trình đọc không render, vẫn đọc được source trong khối code.

| ID | Nội dung |
|---|---|
| D01 | [Tổng quan kiến trúc](diagrams/01-architecture-overview.md) |
| D02 | [Component và hướng phụ thuộc](diagrams/02-component-dependencies.md) |
| D03 | [Ports và lớp OOP](diagrams/03-ports-classes.md) |
| D04 | [Mô hình thực thể (ER)](diagrams/04-entity-relationship.md) |
| D05 | [Ranh giới tin cậy multi-merchant](diagrams/05-multi-merchant-trust.md) |
| D06 | [Sequence: thanh toán thành công](diagrams/06-sequence-success.md) |
| D07 | [Sequence: lỗi, trùng và retry](diagrams/07-sequence-failure-retry.md) |
| D08 | [Sequence: đối soát qua API](diagrams/08-sequence-reconcile.md) |
| D09 | [Vòng đời PaymentIntent](diagrams/09-state-payment-intent.md) |
| D10 | [Vòng đời Inbox và kết quả matching](diagrams/10-state-inbox-transaction.md) |
| D11 | [Pipeline xử lý](diagrams/11-processing-pipeline.md) |
| D13 | [Vòng đời mẫu mã thanh toán theo project](diagrams/13-reference-pattern-lifecycle.md) |
| D12 | [Hai phương án tích hợp host](diagrams/12-host-integration.md) |

Các giới hạn mô hình được ghi tại [data-model.md](data-model.md) và [decisions-and-open-questions.md](decisions-and-open-questions.md); không chuyển thẳng sơ đồ thành migration.
