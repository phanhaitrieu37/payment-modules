# payment-module

**English** · [Tiếng Việt](readme.vi.md)

A reusable, self-hosted payment module for Python services: multi-merchant bank-transfer
payments with [SePay](https://sepay.vn) as the first provider. Payers transfer straight into the
merchant's own bank account; the module confirms, stores and reconciles that money, and your
host keeps prices, orders, taxes and entitlements.

**Status:** version `0.1.0`, the first release (import name `payment_module`). It covers
payment intents with a transfer instruction and VietQR payload, signed SePay webhooks, inbox
processing, settlement, the review queue, merchant onboarding, reconciliation against the SePay
API, a PostgreSQL schema, an optional FastAPI router and a background worker. Known limitations
and the four versioned contracts are in [CHANGELOG.md](CHANGELOG.md).

## Guarantees

- **Exact amount only.** A transfer settles an intent only for its exact amount; anything else
  opens a review case, never a partial or extra settlement.
- **Tenant, merchant and environment isolation.** Every lookup and composite foreign key is
  scoped by tenant, merchant and environment (`test` or `live`); a bank account has one owner per
  environment.
- **Durable inbox before ACK.** A webhook is answered 200 only after the delivery is committed;
  duplicates are acknowledged without settling twice, and a database failure answers 500 so the
  provider retries.
- **Safe defaults.** Reconciliation runs `detect_only`; `auto_settle` needs verified evidence.

## Install

Requires Python 3.12+ and PostgreSQL. From the GitHub Release (checksums in `SHA256SUMS` on the
release page):

```sh
uv pip install "payment-module[sqlalchemy,postgres,fastapi,sepay] @ https://github.com/phanhaitrieu37/payment-modules/releases/download/v0.1.0/payment_module-0.1.0-py3-none-any.whl"
```

or from the git tag: `payment-module[...] @ git+https://github.com/phanhaitrieu37/payment-modules@v0.1.0`.
Pick the extras your host needs; leave out `fastapi` if you have no FastAPI app.

## Documentation

- [Getting started](docs/getting-started.md): from install to a paid order, step by step.
- [`examples/saas_host`](examples/saas_host): FastAPI, settlement in the host transaction.
- [`examples/fnb_host`](examples/fnb_host): no web framework, outbox consumer.
- [CHANGELOG.md](CHANGELOG.md): contracts, version policy and known limitations.
- Design documents (Vietnamese), with their reading order: [readme.vi.md](readme.vi.md#thứ-tự-đọc).
