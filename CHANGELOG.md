# Changelog

Every release lists its changes under the four contracts of the
[0.x version policy](integration-and-versioning.md#versioning): Public API, Event schema,
Normalization and DB schema. In 0.x a breaking change to any of them bumps the minor version;
fixes and additive changes bump the patch version.

## 0.1.0 — 2026-09-23

First release of `payment-module` (import `payment_module`).

**Local-only release.** It is published as the annotated git tag `v0.1.0` plus a locally built
wheel and sdist. There is no remote, no GitHub Release and no PyPI or private index. Install the
wheel directly, or pin the tag once the repository has a remote.

**Reproducible build.** `make build` sets `SOURCE_DATE_EPOCH` to the committer time of `HEAD`
and refuses to build when tracked files have uncommitted changes. To rebuild the release
artifacts, build from a clean checkout of the tag, never from another tree:

```sh
git worktree add --detach ../pm-v0.1.0 v0.1.0
cd ../pm-v0.1.0
SOURCE_DATE_EPOCH=$(git log -1 --format=%ct v0.1.0^{commit}) uv build --out-dir dist
shasum -a 256 dist/*
```

The epoch and the sha256 of the wheel and the sdist are recorded in the tag message: see
`git show v0.1.0`.

### Public API

- `build_payment_module(...)` returns a `PaymentModule` that wires every use case: create,
  query and cancel intents; ingest webhooks and process the inbox; dispatch and requeue the
  outbox; resolve and rematch reviews; onboard merchants, receiving accounts, connections and
  bindings; set connection status and reconcile mode; record readiness; create, activate,
  retire and import reference profiles; reconcile against the provider API; purge expired
  payloads.
- Ports for the host: unit of work, clock, metrics, handlers and `OutcomeObserver`, publisher,
  provider and reader, secret and connection resolvers, reference generator, template checklist and
  `EvidenceVerifier` (with `FileEvidenceVerifier` as the file-based adapter).
- Optional adapters behind extras: `sqlalchemy` (tables, repositories, unit of work),
  `postgres` (asyncpg), `fastapi` (webhook router), `sepay` (provider, API v2 reader,
  checklist, VietQR).
- Safe defaults: `reconcile_mode = detect_only`, `timestamp_tolerance_seconds = 300`
  (allowed 60–7200); `auto_settle` needs a valid `reconcile_evidence_ref`.

### Event schema

- Outbox events `PaymentSettled`, `PaymentNeedsReview` and `ReviewResolved`, all with
  `schema_version = 1`. The contract key is `(event_type, schema_version)`: adding a field does
  not bump the version, removing or redefining one does. Consumers deduplicate by `event_id`.

### Normalization

- SePay webhook bodies and API v2 rows normalize to one observation shape. The receiving account
  key is `bank|account_number|sub_account`.
- `transferType` is whitelisted to `in` and `out` (any case); anything else, or a missing value,
  becomes `unknown` and no money direction is inferred from it.
- The provider `transactionDate` is not used; lateness is decided by server receive time.

### DB schema

- `schema_v1`: every table uses the `pm_` prefix, is defined by
  `define_tables(metadata, prefix="pm_")` and is created by the frozen migration
  `schema_v1.upgrade`. The host runs migrations; nothing auto-stamps.
- The schema is frozen from this release on. A later change ships as `schema_v2` with an upgrade
  step and a migration note, never as an edit to `schema_v1`.

### Known limitations

- **`auto_settle` stays locked.** SePay Test scenario (a) is INCONCLUSIVE: 5 of 5 ACB pairs had
  equal, non-empty bank references (8 of 8 across the run), but the rule needs at least 20 pairs
  per gateway, produced by analyzer `sepay_probe.analyze/1` and at most 180 days old. The
  committed verdict is rejected by the evidence verifier with `SCENARIO_A_NOT_PASS`. Evidence
  decisions: [`plans/reports/sepay-evidence-decisions-260923.md`](plans/reports/sepay-evidence-decisions-260923.md).
  Unlocking needs no new release: an operator sets `auto_settle` with a valid evidence file.
- **Live `auto_settle` is out of scope for 0.x.** The evidence verifier requires the evidence
  environment to equal the connection environment, and the analyzer only produces `test`
  evidence. Live connections therefore run `detect_only`; webhook settlement is unaffected.
  Producing Live evidence is a future design decision.
- **The shipped synthetic evidence fixture expires.** `tests/fixtures/sepay-test-verification.example.json`
  was generated at 2026-09-21 16:00 UTC+7 and becomes stale after 2027-03-20 16:00 UTC+7
  (180 days, `max_age_days`). From then on the two-host reuse acceptance test and the SaaS host
  smoke (`SystemClock`, `examples/saas_host/app.py:52`) fail with `STALE`. The repair is
  deferred to 0.1.1.
- **Two layers of fixtures.** `tests/fixtures/sepay/*.json` are anonymized payloads observed on
  SePay Test. `tests/fixtures/sepay/doc_derived/` comes from the SePay documentation and is kept
  because an outgoing webhook has not been observed yet.
- **Outgoing webhook not observed.** The API reports `transfer_type = out` with only
  `amount_out`, but SePay sent no webhook for the outgoing transfer, so the webhook value is
  unknown and falls back to `unknown`.
- **Account identity only on ACB.** The account key and VA shape were confirmed on ACB Test for
  the main account and one VA; other gateways have no evidence yet.
- **`webhook_success` is not a delivery signal.** It was 1 for a transfer the code filter never
  delivered and for one that met a 503. Linking relies on the grace period only, and the metric
  `reconcile_webhook_success_but_missing_total` is informational (it counts filtered
  transactions too).
- **Signing timestamp lag.** In this single Test run, `X-SePay-Timestamp` was 151.8–153.9 s
  (about 152 s) behind receipt on every delivery. The single observed retry, of one
  transaction, carried a refreshed timestamp. The 300 s tolerance keeps about 146 s of headroom.
  Only 2 attempts of that transaction, 62.5 s apart, were observed during a 15-minute outage;
  the documented 1 + 7 retry schedule was not observed.
- **Release channel.** Local-only (see above); digests are in `git show v0.1.0`, not in this file.
- **Follow-ups for 0.1.1.** The module docstring of `payment_module.adapters.sepay.payload`
  still says the field names are unverified, although ACB Test confirmed them. `analyze.py`
  only reproduces scenario (a); scenarios (b)–(e) of the verdict were analyzed manually (see the
  evidence decisions). The synthetic fixture expiry above is repaired in 0.1.1.
