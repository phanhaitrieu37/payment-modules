# Phase 04 — Schema SQLAlchemy/PostgreSQL, migration đóng băng, UoW

## Context Links

- [plan.md](plan.md) · [Phase 02](phase-02-domain-core-and-ports.md)
- Kongming §1, §2, §3, §6, §7: `/Users/trieuphan/source_code/payment_modules/payment_modules_v1/plans/reports/kongming-260921-1734-payment-design-fix-options.md`
- `/Users/trieuphan/source_code/payment_modules/payment_modules_v1/diagrams/04-entity-relationship.md` (đã sửa ở phase 01)
- > **Tham chiếu hành vi (chỉ đọc, không sao chép — quyết định N1 21/09/2026).** MeowAI (`git -C /Users/trieuphan/source_code/stk-meowai/MeowAI show origin/main:<path>`) dùng để đối chiếu *hành vi và biên ca*, không phải nguồn code. Mọi code, hằng số, tên hàm, docstring, bảng dữ liệu test trong package được viết từ tài liệu thiết kế + tài liệu SePay/NAPAS/EMVCo. Không copy-paste, không "port từng hàm", không import, không ghi output của MeowAI làm hằng số test nếu chưa được kiểm chéo bằng nguồn độc lập. Lý do: package phát hành độc lập, ranh giới sở hữu rõ (MeowAI là repo riêng: gốc DeerFlow MIT + code riêng của chủ dự án); reviewer từ chối PR có đoạn giống nguyên văn.
- Yêu cầu generic: package phải chạy được với host dùng Alembic (`create_all + stamp` trên DB rỗng, `schema_v1.upgrade` trên DB có sẵn) — chứng minh bằng `examples/saas_host`.

## Overview

- Priority: P0
- Status: pending
- Mô tả: 15 bảng `pm_*` bằng SQLAlchemy Core qua `define_tables(metadata, prefix)`, ràng buộc DB cho mọi bất biến tiền (unique, CHECK, composite FK theo tenant), migration DDL đóng băng `schema_v1`, repository + `SqlAlchemyUnitOfWork` async, claim inbox/outbox bằng `FOR UPDATE SKIP LOCKED`. Fixture Postgres disposable cho test.

## Key Insights

- Core (không ORM declarative) để không đụng registry mapper của host; host truyền `MetaData` của mình → `create_all` và autogenerate của host thấy bảng.
- Migration không được sinh từ định nghĩa "sống": `schema_v1.upgrade(op, prefix)` là DDL viết tay đóng băng; test parity trong package so `create_all(define_tables)` với `schema_v1.upgrade` trên Postgres. Schema v2 sau này thêm `schema_v2.upgrade_from_v1`.
- DDL giữ portable theo quy ước: không hàm riêng Postgres trong CHECK, JSON qua `with_variant(JSONB)`, partial index khai báo `postgresql_where`. **Chỉ Postgres được test.**
- Tenant là chuỗi do host cấp; `pm_reference_profiles` và `pm_reference_profile_prefixes` là cấu hình cấp project nên **không** có `tenant_id`.
- Nhiều prefix có tên (quyết định người dùng D1) dùng **bảng con** thay vì JSON: DB tự bảo đảm prefix không trùng trong một version (`UQ(profile_version, prefix)`) và độ dài 2–5 bằng CHECK di động. Luật 'không lồng nhau' trong version (`SUB` vs `SUBX`) không biểu diễn được bằng constraint di động → `check_prefix_overlap` kiểm ở application lúc tạo profile (phase 06); giữa các version chỉ có advisory (§1 kongming 2158).
- Schema đóng băng chỉ sau gate phase 01: chủ sở hữu tài khoản/VA trong một installation, scope canonical `tenant + environment + provider + account`, provenance và transaction/lease đã có quyết định. Một `bank_reference` không được làm unique; nó chỉ là ứng viên để link khi đủ bằng chứng.

## Requirements

Bảng và ràng buộc chính (tên cột là hợp đồng v1):

| Bảng | Cột chính | Ràng buộc |
|---|---|---|
| `pm_merchants` | id, tenant_id, host_merchant_ref, status, created_at | UQ(tenant_id,id); UQ(tenant_id,host_merchant_ref) |
| `pm_receiving_accounts` | id, tenant_id, merchant_id, environment {test,live}, bank_code, bank_bin NULL, account_number (đầy đủ), sub_account NOT NULL DEFAULT '', account_number_masked, holder_name, account_fingerprint, provider_account_ref NULL, status | **UQ(environment,account_fingerprint)** (không scope tenant — một chủ sở hữu mỗi environment); UQ(tenant_id,merchant_id,environment,id) (đích FK của binding, intent, fact); FK(tenant_id,merchant_id) |
| `pm_provider_connections` | id, tenant_id, merchant_id, provider, environment {test,live}, locator, secret_ref, api_credential_ref NULL, auth_mode, reconcile_mode DEFAULT 'detect_only', reconcile_evidence_ref NULL, timestamp_tolerance_seconds DEFAULT 300, status DEFAULT 'pending', status_changed_by/at/reason | UQ(locator); UQ(tenant_id,merchant_id,environment,id) (binding); UQ(tenant_id,environment,id) (readiness, observation); UQ(tenant_id,id) (inbox, reconciliation run); FK(tenant_id,merchant_id); CHECK(reconcile_mode='detect_only' OR reconcile_evidence_ref IS NOT NULL); CHECK(timestamp_tolerance_seconds BETWEEN 60 AND 7200) |
| `pm_connection_account_bindings` | tenant_id, merchant_id, environment, connection_id, receiving_account_id, created_by, created_at | PK(connection_id,receiving_account_id); FK(tenant_id,merchant_id,environment,connection_id)→connections; FK(tenant_id,merchant_id,environment,receiving_account_id)→accounts |
| `pm_reference_profiles` | version PK, kind {generated,legacy_import}, suffix_length NULL, alphabet NULL, status {draft,active,accepted_legacy,retired}, created_at, activated_at/by, retired_at/by | partial UQ(status) WHERE status='active'; CHECK(kind='legacy_import' OR (suffix_length BETWEEN 1 AND 30 AND alphabet IS NOT NULL)) |
| `pm_reference_profile_prefixes` | profile_version, name, prefix, created_at | PK(profile_version,name); UQ(profile_version,prefix); FK(profile_version)→`pm_reference_profiles`; CHECK(length(prefix) BETWEEN 2 AND 5); CHECK(length(name) BETWEEN 1 AND 32) |
| `pm_connection_reference_readiness` | id, tenant_id, connection_id, profile_version, environment, status {pending,ready,retired}, checklist JSON, evidence_ref, verified_by, verified_at | UQ(connection_id,profile_version,environment); **FK(tenant_id,environment,connection_id)→connections(tenant_id,environment,id)** (environment của readiness = environment của connection); FK(profile_version) |
| `pm_payment_intents` | id, tenant_id, merchant_id, environment, receiving_account_id, amount_vnd, currency 'VND', beneficiary_snapshot JSON, payment_reference, reference_profile_version, reference_prefix_name NULL (NULL khi `reference_override` với profile `legacy_import`), host_ref_type, host_ref_id, idempotency_key, request_fingerprint, expires_at, status, superseded_by_intent_id NULL, cancel_reason NULL, created_at, paid_at NULL | CHECK(amount_vnd>0); UQ(payment_reference); UQ(tenant_id,idempotency_key,environment); UQ(tenant_id,merchant_id,environment,id) (supersession); UQ(tenant_id,environment,id) (review candidate); UQ(tenant_id,id,amount_vnd,receiving_account_id,environment) (settlement); FK(tenant_id,merchant_id)→merchants (merchant không có environment); FK(tenant_id,merchant_id,environment,receiving_account_id)→accounts; FK(reference_profile_version)→profile; FK(reference_profile_version,reference_prefix_name)→`pm_reference_profile_prefixes` khi non-legacy; self-FK(tenant_id,merchant_id,environment,superseded_by_intent_id); IX(receiving_account_id,payment_reference) |
| `pm_webhook_inbox` | id, tenant_id, connection_id, event_key, event_key_kind {provider_id,body_hash}, body_sha256, raw_body LargeBinary NULL, headers JSON (allowlist), received_at, status, attempts, next_attempt_at, lease_owner, lease_until, **lease_generation BIGINT NOT NULL DEFAULT 0**, last_error_code, purge_after NULL, raw_purged_at NULL | UQ(connection_id,event_key); UQ(tenant_id,connection_id,id) (đích observation); IX(status,next_attempt_at); FK(tenant_id,connection_id)→connections(tenant_id,id) |
| `pm_reconciliation_runs` | id, tenant_id, connection_id, window_from, window_to, cursor, status, counts JSON, started_at, finished_at, last_error | UQ(tenant_id,connection_id,id) (đích observation); FK(tenant_id,connection_id)→connections(tenant_id,id) |
| `pm_provider_observations` | id, tenant_id, **environment NOT NULL** (từ connection), connection_id, provider, source {webhook,api}, source_tx_id, inbox_id NULL, reconciliation_run_id NULL, reported_account_key, bank_reference NULL, amount_vnd, direction {in,out,unknown}, occurred_at NULL, code NULL, memo NULL, normalized JSON, transaction_id NULL, link_method NULL, link_status {linked,unlinked,ambiguous}, observed_at, purge_after NULL | UQ(provider,source,source_tx_id,connection_id); CHECK((source='webhook' AND inbox_id IS NOT NULL) OR (source='api' AND reconciliation_run_id IS NOT NULL)); FK(tenant_id,environment,connection_id)→connections(tenant_id,environment,id); FK(tenant_id,connection_id,inbox_id)→inbox(tenant_id,connection_id,id); FK(tenant_id,connection_id,reconciliation_run_id)→runs(tenant_id,connection_id,id); FK(tenant_id,environment,provider,reported_account_key,transaction_id)→transactions(tenant_id,environment,provider,provider_account_key,id); IX(link_status) |
| `pm_provider_transactions` | id, tenant_id, merchant_id NULL, environment, provider, provider_account_key, receiving_account_id NULL, identity_kind {webhook_id,api_id}, identity_value, dedup_key, webhook_tx_id NULL, api_tx_id NULL, bank_reference NULL, amount_vnd NOT NULL, direction NOT NULL, occurred_at NULL, memo NULL, first_source {webhook,reconcile}, match_state, duplicate_of_transaction_id NULL, created_at, purge_after NULL | **UQ(tenant_id,environment,dedup_key)** (không unique toàn cục); partial UQ(tenant_id,environment,provider,provider_account_key,webhook_tx_id) WHERE webhook_tx_id IS NOT NULL; partial UQ(tenant_id,environment,provider,provider_account_key,api_tx_id) WHERE api_tx_id IS NOT NULL; IX(bank_reference) (không unique); UQ(tenant_id,environment,id) (duplicate-of, review case); UQ(tenant_id,environment,provider,provider_account_key,id) (observation); UQ(tenant_id,id,amount_vnd,receiving_account_id,environment) (settlement); CHECK(amount_vnd>=0); **CHECK((merchant_id IS NULL) = (receiving_account_id IS NULL))**; CHECK(match_state<>'settled' OR (receiving_account_id IS NOT NULL AND direction='in')); FK(tenant_id,merchant_id,environment,receiving_account_id)→accounts; self-FK(tenant_id,environment,duplicate_of_transaction_id) |
| `pm_settlements` | id, tenant_id, environment, transaction_id, intent_id, receiving_account_id NOT NULL, amount_vnd NOT NULL, intent_amount_vnd NOT NULL, origin {auto,operator_review}, review_case_id NULL, resolved_by NULL, settled_at | UQ(transaction_id); UQ(intent_id); CHECK(amount_vnd = intent_amount_vnd); CHECK(origin='auto' OR (review_case_id IS NOT NULL AND resolved_by IS NOT NULL)); FK(tenant_id,environment,transaction_id,amount_vnd,receiving_account_id)→transactions; FK(tenant_id,environment,intent_id,intent_amount_vnd,receiving_account_id)→intents; FK(review_case_id,tenant_id,transaction_id)→review_cases(id,tenant_id,transaction_id) |
| `pm_review_cases` | id, tenant_id, **environment NOT NULL**, transaction_id, candidate_intent_id NULL, reason, details JSON, status {open,resolved}, resolution NULL, resolution_ref NULL, resolved_by NULL, resolution_note NULL, opened_at, resolved_at NULL | partial UQ(transaction_id) WHERE status='open'; UQ(id,tenant_id,transaction_id) (đích settlement); FK(tenant_id,environment,transaction_id)→transactions(tenant_id,environment,id); FK(tenant_id,environment,candidate_intent_id)→intents(tenant_id,environment,id) |
| `pm_outbox_events` | event_id PK, tenant_id, type, schema_version, aggregate_type, aggregate_id, payload JSON, trusted_scope JSON, status {pending,published,failed}, attempts, **lease_owner NULL, lease_until NULL, lease_generation BIGINT NOT NULL DEFAULT 0**, occurred_at, available_at, next_attempt_at, last_error NULL, published_at NULL | IX(status,next_attempt_at) |

Repository/UoW:
- Ràng buộc trong bảng trên khớp `data-model.md` → "Ma trận sở hữu, ràng buộc và test" (O1–O18) và bảng "đích unique cho composite FK"; khi khác nhau, theo `data-model.md`. Quyết định người dùng 22/09/2026: một tài khoản/VA thuộc đúng một `tenant + merchant` mỗi environment (`UQ(environment,account_fingerprint)`). PostgreSQL đòi FK trỏ tới unique trên đúng tập cột được tham chiếu: trước khi viết DDL, liệt kê mọi FK cùng đích unique vào docstring `tables.py`; parity test so cả danh sách này. Test SQL trực tiếp cả hai merchant cùng tenant và Test/Live.
- Canonical fact, account, binding, intent, observation, readiness, review case và settlement đều mang `environment` trong composite FK/unique tương ứng, để fact Test không thể gắn vào intent Live dù cùng tenant/merchant/account. Dedup trong `UQ(tenant_id,environment,dedup_key)`; partial unique ID theo tenant/environment/provider/account. Hai connection chỉ được cùng link một fact khi scope tenant, environment, provider và account khớp. Không dùng `bank_reference` làm unique.
- FK có cột nullable dùng MATCH SIMPLE (bỏ kiểm tra khi có cột NULL): `CHECK((merchant_id IS NULL) = (receiving_account_id IS NULL))` ở `pm_provider_transactions` chặn đường vòng qua FK receiver. Observation `transaction_id NULL`, review `candidate_intent_id NULL`, intent `superseded_by_intent_id NULL` và fact `duplicate_of_transaction_id NULL` là chủ đích. Cặp `(reference_profile_version, reference_prefix_name)` với name NULL chỉ hợp lệ cho profile `legacy_import` — không biểu diễn được bằng CHECK, application kiểm (phase 05); ghi chú trong docstring.
- `pm_payment_intents.reference_profile_version` có FK riêng tới profile ngay cả khi `reference_prefix_name=NULL` cho legacy; `superseded_by_intent_id` có self-FK theo tenant + merchant + environment, `duplicate_of_transaction_id` theo tenant + environment. Settlement có composite FK tới review case đúng tenant và transaction; `origin='operator_review'` đòi `review_case_id` cùng `resolved_by`, auto không mạo danh provenance operator. Các cột `headers`/`normalized` dự kiến purge ở phase 08 phải nullable sau purge và parity test phản ánh đúng.
- Inbox/outbox claim có `lease_owner`, `lease_until` và `lease_generation BIGINT NOT NULL DEFAULT 0` (cả hai bảng); claim tăng generation; finalize thành công hoặc lỗi là `UPDATE … WHERE id = ? AND lease_generation = ?` (compare-and-set) để owner cũ không ghi sau khi lease được claim lại. Outbox cũng có đường phục hồi claim hết hạn và publish at-least-once.
- Chuỗi trạng thái lấy từ `payment_module.domain.enums` qua `.value`; không viết literal mới. CHECK cho cột enum dùng `IN (...)` di động.
- `SqlAlchemyUnitOfWork(session_factory, tables)`; lộ `.session` cho handler cùng UoW (hợp đồng của adapter, không phải của port).
- `SqlAlchemyUnitOfWork.joined(session, tables)`: UoW bọc session **của host** — `commit()` chỉ `flush()`, host sở hữu commit/rollback. Dùng khi host cần tạo/huỷ intent nguyên tử cùng order của mình (`examples/saas_host`, phase 08). Test: host rollback → không còn intent.
- Claim: `inbox.claim_batch(limit, lease_seconds, owner, now)` = `SELECT … WHERE ((status IN ('received','retry_wait') AND (next_attempt_at IS NULL OR next_attempt_at<=now)) OR (status='processing' AND lease_until<now)) … FOR UPDATE SKIP LOCKED` rồi set `processing`, `lease_until`, `lease_generation = lease_generation + 1`. Tương tự `outbox.claim_batch`.
- `intents.get_for_update(id)`, `transactions.insert_or_get_by_dedup_key` (INSERT … ON CONFLICT (tenant_id, environment, dedup_key) DO NOTHING RETURNING, fallback SELECT theo cùng khoá).
- Retry sinh reference trong joined UoW: `INSERT … ON CONFLICT DO NOTHING RETURNING`; không insert được thì tra `(tenant_id,idempotency_key)`: fingerprint giống → trả intent cũ, khác → `IdempotencyConflict`, không có → chỉ lúc đó sinh reference mới (tối đa 5 lần). Có thể dùng savepoint cho từng lần thử nếu cần; không bắt `IntegrityError` rồi tiếp tục trong transaction PostgreSQL đã abort, và không nuốt lỗi FK/constraint khác.

## Architecture

```
payment_module.adapters.sqlalchemy
  tables.py            define_tables(metadata, prefix="pm_") -> PaymentTables (namespace các Table)
  migrations/schema_v1.py   upgrade(op, prefix="pm_"), downgrade(op, prefix="pm_")  # DDL đóng băng
  uow.py               SqlAlchemyUnitOfWork, SqlAlchemyUnitOfWorkFactory
  repositories/*.py    một file mỗi aggregate
```

Host độc lập (không Alembic) có thể gọi `await conn.run_sync(metadata.create_all)` cho môi trường dev; production phải dùng Alembic của host gọi `schema_v1.upgrade`.

## Related Code Files

Create (dưới `/Users/trieuphan/source_code/payment_modules/payment_modules_v1/`):
- `src/payment_module/adapters/__init__.py`, `src/payment_module/adapters/sqlalchemy/__init__.py`
- `src/payment_module/adapters/sqlalchemy/tables.py`
- `src/payment_module/adapters/sqlalchemy/migrations/__init__.py`, `schema_v1.py`
- `src/payment_module/adapters/sqlalchemy/uow.py`
- `src/payment_module/adapters/sqlalchemy/repositories/merchants.py`, `receiving_accounts.py`, `connections.py`, `bindings.py`, `reference_profiles.py` (profile + prefix con, đọc/ghi nguyên tử), `readiness.py`, `intents.py`, `inbox.py`, `observations.py`, `transactions.py`, `settlements.py`, `review_cases.py`, `outbox.py`, `reconciliation_runs.py`
- `tests/integration/conftest.py` (fixture `pg_url` từ `testcontainers` `postgres:17`, một container mỗi session; `DATABASE_URL` env ghi đè để dùng server ngoài; marker `postgres`)
- `tests/integration/storage/test_schema_parity.py` (create_all vs `schema_v1.upgrade` qua Alembic `MigrationContext` + `Operations`: bảng, cột, kiểu, index, unique, CHECK, FK)
- `tests/integration/storage/test_money_constraints.py`
- `tests/integration/storage/test_tenant_isolation_constraints.py`
- `tests/integration/storage/test_claim_skip_locked.py`

## Implementation Steps

1. TDD với test ràng buộc trước (fail đỏ), rồi `tables.py`.
2. `test_money_constraints.py`: insert trực tiếp (SQL Core) các ca vi phạm và kỳ vọng `IntegrityError`: settlement lệch tiền với `origin='auto'` **và** `origin='operator_review'`; settlement có receiver khác intent; settlement cho tx `receiving_account_id NULL`; hai settlement cùng intent; hai settlement cùng tx; tx `match_state='settled'` với `direction='out'`; hai review `open` cùng tx; `reconcile_mode='auto_settle'` không có `reconcile_evidence_ref`; hai profile `active`; hai prefix cùng giá trị trong một version; prefix 1 hoặc 6 ký tự; intent trỏ `(profile_version, reference_prefix_name)` không tồn tại.
3. `test_tenant_isolation_constraints.py`: intent tenant A trỏ receiving account tenant B → lỗi FK; review `candidate_intent_id` chéo tenant → lỗi.
   - Thêm direct-SQL negative cases: cùng tenant nhưng khác merchant ở binding/intent/supersession; Test fact trỏ Live intent/settlement dù số tiền, account và mã khớp; observation trỏ connection/inbox/run/fact sai tenant hoặc environment, và trỏ inbox/run của connection khác cùng tenant; observation `environment` khác connection; readiness `live` cho connection `test`; review case trỏ candidate intent chéo environment; operator settlement thiếu review case hoặc case thuộc transaction khác; supersession/duplicate-of chéo tenant hoặc environment; fact `merchant_id = NULL` với `receiving_account_id` khác NULL. Ownership: cùng fingerprint + environment cho tenant khác và merchant khác bị từ chối, Test + Live được nhận.
   - Positive cases dedup scope: cùng `webhook_tx_id` + tài khoản ở Test và Live đều insert được; cùng `dedup_key` ở hai tenant đều insert được.
4. `schema_v1.py` viết tay khớp `tables.py`; `test_schema_parity.py` xanh trên Postgres.
5. Repositories + UoW; `test_claim_skip_locked.py`: hai session song song claim cùng batch → không trùng id; lease hết hạn → claim lại được.
   - Test owner cũ finalize sau reclaim không đổi inbox/outbox; retry reference trong `joined(session)` vẫn giữ transaction host và phân biệt race idempotency với collision; test parity gồm FK, unique, nullability và lease fields mới.
6. Đăng ký marker `postgres` trong `[tool.pytest.ini_options].markers` của `pyproject.toml` trong cùng commit (hiện chỉ có `integration`, `acceptance`, `contract`; không để marker chưa đăng ký). `uv run pytest -m postgres -q`; `uv run ruff check .`; commit `feat(storage): add sqlalchemy schema, frozen migration and unit of work`.

## Todo List

- [ ] Test ràng buộc tiền/tenant (đỏ)
- [ ] `define_tables` 15 bảng (gồm `pm_reference_profile_prefixes`)
- [ ] `schema_v1` đóng băng + parity test
- [ ] Repositories + UoW + claim SKIP LOCKED
- [ ] Composite merchant/provenance/audit FK, canonical scope, lease fencing và retry unique trong joined UoW
- [ ] Đích unique cho mọi composite FK (docstring `tables.py` + parity), dedup theo tenant/environment, CHECK NULL-parity merchant/receiver, environment ở observation/readiness/review case
- [ ] Fixture Postgres disposable

## Success Criteria

- `cd /Users/trieuphan/source_code/payment_modules/payment_modules_v1 && uv run pytest tests/integration/storage -m postgres -q` → pass (cần Docker).
- `uv run pytest tests/integration/storage/test_money_constraints.py -q -k "operator_review_amount_mismatch_rejected"` → pass.
- `uv run python -c "import sqlalchemy as sa; from payment_module.adapters.sqlalchemy.tables import define_tables; m=sa.MetaData(); t=define_tables(m); assert len([n for n in m.tables if n.startswith('pm_')])==15"` → exit 0.
- `grep -rn "variance" src/` → rỗng.
- `uv run pytest tests/integration/storage/test_money_constraints.py -m postgres -q -k "duplicate_prefix_in_version_rejected or prefix_length_rejected"` → pass.
- Direct SQL cross-merchant, cross-environment, provenance/operator/self-FK đều bị từ chối; race claim/reclaim và collision reference trong transaction host có test PostgreSQL xanh. `schema_v1` và `define_tables` có cùng 15 bảng và cùng ràng buộc mở rộng.

## Risk Assessment

- Composite FK nhiều cột + unique tương ứng làm insert chậm hơn: bảng nhỏ, tải thấp (giả định tải thấp; chưa có số liệu); chấp nhận.
- Parity create_all vs migration lệch theo thời gian → test parity chặn trong CI package.
- `ON CONFLICT` là cú pháp riêng dialect → dùng `sqlalchemy.dialects.postgresql.insert`; chỉ Postgres được test.

## Security Considerations

- `account_number` đầy đủ và `raw_body` là dữ liệu nhạy cảm: repository không bao giờ đưa chúng vào `__repr__`/log; view DTO cho host chỉ có `account_number_masked`.
- `secret_ref`/`api_credential_ref` là tham chiếu (vd `env:SEPAY_WEBHOOK_SECRET`), không bao giờ là giá trị secret; test kiểm `RegisterConnection` từ chối chuỗi không có scheme `env:`/`vault:`.
- `purge_after` có sẵn ở inbox/observation/transaction để phase 08 purge PII.

## Next Steps

- Phase 05 dùng UoW; `examples/saas_host` (phase 08) gọi `define_tables(metadata)` + `schema_v1.upgrade` qua Alembic env của host mẫu.
