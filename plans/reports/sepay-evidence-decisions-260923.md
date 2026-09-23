# ADR — SePay evidence decisions for release 0.1.0

Date: 2026-09-23 · Status: accepted · Release: `payment-module` 0.1.0

## Input

- Verdict: [`plans/reports/sepay-test-verification-260922.json`](sepay-test-verification-260922.json)
  (human-readable: [`sepay-test-verification-260922.md`](sepay-test-verification-260922.md)).
- Run `sepay-test-20260922-1736`, environment `test`, `evidence_schema_version` 1,
  `analyzer_version` `sepay_probe.analyze/1`, `generated_at` 2026-09-23T09:27:11+07:00.
- Embedded digest (checked by `FileEvidenceVerifier`):
  `f91149c732a63d530d604d58b3e354ce068f201cbeeff83acdf9c0e0af81c4a0`.
  File sha256 at this decision: `92d56c5169c3850c3e2eca031d4c4d3948a46f27f47ec77b3de7b02ab64426fe`.
- Scope of the evidence: SePay Test only, one ACB account plus one VA, 10 transfers.

## Decisions

A decision only follows the "PASS" branch of the phase decision table when its own scenario is
PASS and the evidence actually supports the stronger claim. Every other status keeps the safe
default. No `src/` change was needed; only `__version__` moved to `0.1.0`.

| Scenario | Status | Decision | Code change |
|---|---|---|---|
| a — bank reference equality | INCONCLUSIVE (5 of 5 ACB pairs equal and non-empty, 8 of 8 across the run; rule needs ≥ 20 pairs per gateway) | Keep `reconcile_mode = detect_only`. The verifier rejects this verdict with `SCENARIO_A_NOT_PASS` (`test_committed_verdict_is_rejected_for_auto_settle`); a forged PASS with 5 pairs is rejected with `GATEWAY_NOT_ELIGIBLE`. `auto_settle` stays locked until a run with ≥ 20 pairs per gateway. | none |
| b — retry during outage | PASS for this run, `timestamp_on_retry = refreshed` from the single observed retry of one transaction; signing lag −151.8 to −153.9 s (about 152 s) on all 9 deliveries of this run; 2 attempts of that transaction, 62.5 s apart | Keep `timestamp_tolerance_seconds = 300` (the `refreshed` branch); about 146 s of headroom. Monitor `webhook_auth_failures_total{reason=stale_timestamp}`. The documented 1 + 7 retry schedule was not observed. | none |
| c — outgoing money direction | INCONCLUSIVE (API `transfer_type = out` with only `amount_out`; no outgoing webhook was sent) | Keep the `transferType` whitelist `in`/`out`; anything else becomes `unknown` (`adapters/sepay/payload.py`). Keep `tests/fixtures/sepay/doc_derived/` for the outgoing webhook. | none |
| d — `webhook_success` meaning | PASS, but `usable_as_delivery_signal = false` (1 for a filtered, never-sent transfer and for a transfer that met 503) | Grace-only branch: never link or settle on this field. `reconcile_webhook_success_but_missing_total` is informational only (it also counts filtered transactions). | none |
| e — account identity | PASS on ACB: `bank\|account_number\|sub_account` equal in webhook and API for main account and VA; `bank_account_id` does not distinguish the VA | Keep `account_key` as implemented; evidence covers ACB only. | none |

## Provenance of the verdict

Only scenario (a) is reproducible from the committed analyzer: `python -m tools.sepay_probe.analyze`
on the raw capture gives the same `by_gateway` block as the verdict. Scenarios (b)–(e) were
analyzed manually from the raw capture and the API export; the committed `analyze.py` does not
produce them. An independent codex verification cross-checked those manual conclusions. Only
scenario (a) gates behaviour (the verifier reads nothing else); (b)–(e) inform the defaults and
documentation in the table above. Extending `analyze.py` to (b)–(e) is scheduled for 0.1.1.

## Live connections

Live `auto_settle` is out of scope for 0.x. `FileEvidenceVerifier` requires the evidence
`environment` to equal the connection environment, and the analyzer only writes
`environment = "test"`. Live connections therefore run `detect_only`; webhook settlement is
unaffected. How Live evidence would be produced is a future design decision.

## Evidence portability

The wheel-only path is already covered: `tests/integration/acceptance/test_reuse_two_hosts.py`
installs the built wheel into two venvs and the SaaS host resolves an evidence file through
`EVIDENCE_ROOT` + `FileEvidenceVerifier`, switching to `auto_settle`. That artifact is the
**synthetic** fixture `tests/fixtures/sepay-test-verification.example.json` (20 VCB pairs); the
real verdict above is rejected, as intended.

## Consequences

- Release 0.1.0 ships with `detect_only` and 300 s; the CHANGELOG lists these limits.
- A later Test run with ≥ 20 pairs per gateway can unlock `auto_settle` for that gateway without
  any release: it is an operator action (`SetReconcileMode` with an `evidence_ref` to a valid
  evidence file). A release is needed only if that run changes the normalization or fixtures.
- The synthetic evidence fixture `tests/fixtures/sepay-test-verification.example.json`
  (generated 2026-09-21 16:00 UTC+7) becomes stale after 2027-03-20 16:00 UTC+7 (180 days,
  `max_age_days`). From then on the two-host reuse acceptance test and the SaaS host smoke
  (`SystemClock`, `examples/saas_host/app.py:52`) fail with `STALE`. The repair is deferred to
  0.1.1.
- Raw capture under `.evidence/sepay-test/sepay-test-20260922-1736` holds real account data and
  is not committed; delete it once the verdict is accepted.
