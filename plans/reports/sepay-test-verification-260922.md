# SePay Test verification — run `sepay-test-20260922-1736`

Environment: SePay **Test** only. Run window: 22/09/2026 17:46 to 23/09/2026 09:17 (Vietnam time, UTC+7).
Machine-readable verdict: [`sepay-test-verification-260922.json`](sepay-test-verification-260922.json)
(`evidence_schema_version` 1, `analyzer_version` `sepay_probe.analyze/1`, digest checked by
`FileEvidenceVerifier`).

Every figure below was recomputed from the raw capture and API dumps under
`.evidence/sepay-test/sepay-test-20260922-1736/`, which is gitignored and never committed. The report
contains no account number, VA string, bank reference, SePay id or name. Masked placeholders such as
`ACCOUNT_1` and `VA_1` stand for the real values, and equal placeholders stand for equal real values.

## Verdict

| Scenario | What was run | Status | One-line result |
|---|---|---|---|
| a | 5 inbound transfers of 10 001 to 10 005 VND on the main account | **INCONCLUSIVE** | 5 of 5 pairs have equal, non-empty bank references, but the rule needs at least 20 pairs. The user chose 5. |
| b | Endpoint answered 503 for 15 min 9 s while a 10 006 VND transfer arrived | **PASS** | 2 attempts, 62.5 s apart, with a refreshed `X-SePay-Timestamp`. For this transfer no further receipt arrived before capture stopped about 15 h later. |
| c | One outgoing transfer of 10 007 VND | **INCONCLUSIVE** | The API gives `transfer_type` `out` with only `amount_out`. No webhook was sent, so the webhook value for outgoing money was not observed. |
| d | Code-only filter on, a no-code 10 010 VND transfer | **PASS** | No webhook arrived up to capture stop, yet `webhook_success` was 1 right away and still 1 at the API recheck 15 h 2 m 25 s later. |
| e | 10 008 VND to a VA, 10 009 VND to the main account | **PASS** | `bank\|account_number\|sub_account` is identical in the webhook and the API for both transfers. |

## Evidence inventory

- The capture recorded 11 receipts on `/sepay`. 9 of them were HMAC-valid SePay deliveries of 8 distinct
  transactions. The extra receipt is the retry in (b), with the same id and the same body hash.
- 2 receipts had no valid HMAC, no signature timestamp and no id. Both were controller connectivity
  checks and are excluded from every count. The first one (28-byte body, 17:46:52) was a reachability
  check. The second one (2-byte body, 17:59:24) was sent at the moment 503 mode started, to check it.
- There were 6 API v2 reads, each on one page (`has_more` false, `last_page` 1, `per_page` 100): 22, 26, 28, 30, 31
  and 31 rows. The run added 10 rows, 10 001 to 10 010 VND, all on one ACB account. Apart from
  `webhook_success`, no row changed between reads, and `webhook_success` itself never changed either.
- Webhook `transactionDate` and API `transaction_date` use the format `YYYY-MM-DD HH:mm:ss` with no offset,
  in Vietnam time. They are equal on every pair, and 2.6 s to 65.9 s before the local receipt time.

## Scenario details

### (a) Bank reference equality — INCONCLUSIVE

Each webhook was paired with an API row by a unique amount, then checked for the same account, direction
and transaction date. Only after pairing were the bank references compared.

| Gateway | pairs_total | equal, non-empty | mismatch | empty | auto_settle_eligible |
|---|---|---|---|---|---|
| ACB | 5 | 5 | 0 | 0 | false |

- Across the whole run there were 8 delivered pairs (the 5 from (a), the (b) transfer and the 2 from (e)).
  All 8 have equal, non-empty references. They are not counted toward the 20-pair rule because they were
  not part of scenario (a).
- Webhook `referenceCode` equals API `reference_number`. Every value has 14 characters: `SB` followed by
  12 upper-case hex digits.
- Webhook `content` equals API `transaction_content` in 5 of 5 pairs for (a), and in 8 of 8 pairs for the
  whole run. `description` carries the same text as `content`.
- `code` is an empty string in both sources for every test transfer, because no code pattern was used.
- Webhook ids are integers and API ids are UUIDs. No webhook id appears anywhere in any API row, so the
  two id spaces do not overlap and cannot link the sources.

### (b) Retry during an outage — PASS

| Attempt | Received (UTC) | X-SePay-Timestamp minus receipt | Answered | Body |
|---|---|---|---|---|
| 1 | 11:04:19.435 | −152.4 s | 503 | first |
| 2 | 11:05:21.914 | −153.9 s | 503 | same hash |

- `timestamp_on_retry` is `refreshed`. The two headers are 61 s apart, and the receipts are 62.48 s apart.
- The 503 window ran from 10:59:24 to 11:14:33 UTC. After attempt 2 this endpoint received nothing more
  for this transfer: not in the remaining 9 minutes of the window, not after it answered 200 again, and
  not up to capture stop at 02:17:25 UTC the next day (about 15 h later). The transfer was not delivered
  within that observation window.
- The SePay documentation read earlier describes one attempt plus seven Fibonacci retries over about
  33 minutes. For this one transfer, with 503 answers, this endpoint saw two attempts 62.48 s apart and
  no further receipt before capture stopped. One transfer on one endpoint does not establish SePay's
  retry policy in general.
- `recommended_tolerance_seconds` is 300, the phase rule for `refreshed` timestamps. The skew section
  gives the measured boundary separately.

### (c) Outgoing money — INCONCLUSIVE

- API row: `transfer_type` is `out`, `amount_in` is 0 and `amount_out` is 10007. Amounts are JSON
  integers. The API row carries the fields `account_number`, `accumulated`, `amount_in`, `amount_out`,
  `bank_account_id`, `bank_brand_name`, `code`, `id`, `reference_number`, `transaction_content`,
  `transaction_date`, `transfer_type`, `va`, `va_id` and `webhook_success`.
- Across all 31 API rows, `transfer_type` takes only the values `in` and `out`. It always agrees with
  the adapter's amount rule: `in` exactly when only `amount_in` is positive, `out` exactly when only
  `amount_out` is.
- No webhook was sent for the outgoing transfer, so the only `transferType` seen in webhooks is `in`.
  The run cannot tell whether SePay never sends outgoing webhooks or whether the Test webhook was set to
  send incoming money only.

### (d) `webhook_success` semantics — PASS

| Transfer outcome at this endpoint | `webhook_success` |
|---|---|
| Filtered by the code-only rule, never sent (10 010) | 1 (right away and at the API recheck 54 145 s, 15 h 2 m 25 s, later) |
| Two attempts, both answered 503, never delivered (10 006) | 1 |
| Delivered and answered 200 (10 001 to 10 005, 10 009) | 2 |
| Delivered and answered 200, VA transfer (10 008) | 1 |
| Outgoing, no webhook (10 007) | 0 |

For the filtered transfer, the transfer was confirmed at 11:28:22 UTC, the capture saw no webhook for
it until capture stop at 02:17:25 UTC (53 343 s, 14 h 49 m 3 s), and the API recheck ran at 02:30:47 UTC
(54 145 s, 15 h 2 m 25 s, after confirmation).

The value does not tell whether this endpoint received the transfer. A value of 1 appears on a transfer
that was never sent, on one that was never delivered, and on one that was delivered. The value could
count successful deliveries across every webhook configured on the account. The run did not record how
many webhooks the Test account has, so that explanation is not confirmed.

### (e) Account key, VA versus main account — PASS

| Field | VA transfer | Main-account transfer |
|---|---|---|
| webhook `gateway` / API `bank_brand_name` | ACB / ACB | ACB / ACB |
| webhook `accountNumber` / API `account_number` | ACCOUNT_1 / ACCOUNT_1 | ACCOUNT_1 / ACCOUNT_1 |
| webhook `subAccount` / API `va` | VA_1 / VA_1 | "" / "" |
| API `va_id` | VA_ID_1 | null |
| API `bank_account_id` | BANK_ACCOUNT_ID_1 | BANK_ACCOUNT_ID_1 |

- A VA transfer carries the main account number, with the VA in `subAccount` and `va`.
- A missing sub-account is an empty string in both sources, not null.
- `bank_account_id` is the same for the VA and the main account, so it cannot tell them apart.
  `bank|account_number|sub_account` does tell them apart.
- The gateway string is `ACB` in both sources.

## Timestamp skew

For all 9 HMAC-valid deliveries, `X-SePay-Timestamp` minus the local receipt time is between −153.9 s
and −151.8 s (mean −152.5 s). The maximum observed absolute skew is 153.948 s.

The capture-host clock was measured against NTP with `sntp` (`clock_check.txt` in the raw evidence
directory): +0.049 s against both time.apple.com and pool.ntp.org at 2026-09-22T10:59:12Z, between
scenarios (a) and (b), and +0.106 s at the re-check on 2026-09-23T02:39:33Z. The capture clock was
therefore within about 0.1 s of NTP, and `transactionDate` agrees with it to within a few seconds. The
lag of about 2.5 minutes is on SePay's signing timestamp, on first attempts and retries alike.

In this run, any tolerance below 153.948 s would have rejected at least one valid delivery. That is the
measured boundary for these 9 deliveries. It is not a guaranteed upper bound on SePay's lag.

## What this means for the package

- **Timestamp tolerance.** Keep `timestamp_tolerance_seconds` at the 300 s default. This is an
  operational recommendation: it leaves about 146 s of headroom over the largest skew observed
  (153.948 s). Separately, the measured boundary is that a tolerance below 153.948 s would have rejected
  at least one valid delivery in this run, so the configured minimum of 60 s is unsafe for SePay. The
  retry observed in (b) carried a fresh timestamp, so for that retry the tolerance did not need to cover
  the time since the first attempt.
- **auto_settle stays off.** The verdict's gateway entry has 5 equal pairs and `auto_settle_eligible`
  false, and scenario (a) is INCONCLUSIVE. `FileEvidenceVerifier` rejects this artifact
  (`SCENARIO_A_NOT_PASS`, and `GATEWAY_NOT_ELIGIBLE` even if (a) were forced to PASS). Every connection
  stays `detect_only` until a run with at least 20 equal pairs per gateway.
- **`webhook_success` is unusable as a delivery signal.** Reconciliation must not read it as "the host
  received this transfer". The API reader has to compare API rows with stored observations itself.
- **Retry window and recovery.** In (b), after two 503 answers 62.48 s apart, this endpoint received
  nothing more for that transfer before capture stopped about 15 h later. The package must not rely on
  provider retries to recover deliveries missed during an outage or a 5xx answer. API reconciliation is
  the required recovery mechanism, so its schedule and lookback must cover whole outages rather than
  the 33-minute retry schedule described in the documentation. This run does not establish SePay's
  retry policy beyond that one observed transfer.
- **Cross-source linking.** For every observed pair, the webhook and the API row share the bank
  reference (`referenceCode` = `reference_number`), the amount, the account key, the direction, the
  transaction date and the memo (`content` = `transaction_content`). The ids are disjoint, with integers
  in webhooks and UUIDs in the API. Linking by bank reference plus a memo-token check is supported by
  8 of 8 pairs. This is still a small sample and does not prove that bank references are globally
  unique.
- **Direction whitelist.** The API values `in` and `out` are confirmed. The webhook value for outgoing
  money is unconfirmed, so any other `transferType` must keep mapping to `unknown`.
- **Fixtures.** `tests/fixtures/sepay/webhook_in.json`, `webhook_in_va.json` and
  `api_transactions.json` are anonymized copies of the observed (e) and (c) payloads, marked
  `"_provenance": "observed: …"`. `tests/contract/sepay/test_observed_payloads.py` checks that the
  adapter normalizes them. The documentation-derived fixtures stay in `tests/fixtures/sepay/doc_derived/`.

## Dashboard state at the end of the run

These are the facts the controller recorded, verbatim:

- `dashboard_final_state.txt`: "webhook code filter disabled by user at 2026-09-22T11:32:17Z", which is
  18:32:17 Vietnam time.
- `mode`: `accept`. The capture server was stopped at 2026-09-23T02:17:25Z, and the run is closed.

No record says whether the Test webhook URL still points at the stopped capture tunnel. If it does,
every later Test transfer will fail delivery until the URL is changed or removed.

## Deleting the raw capture

Delete the raw capture once the phase is accepted and nobody needs to re-derive the verdict. It contains
real account numbers, the VA string, bank references and SePay ids.

```sh
cd /Users/trieuphan/source_code/payment_modules/payment_modules_v1
rm -rf .evidence/sepay-test/sepay-test-20260922-1736
rmdir .evidence/sepay-test 2>/dev/null || true
git ls-files .evidence            # must print nothing
git status --short --ignored .evidence
```

## Unresolved questions

1. Does SePay Test send webhooks for outgoing money when the webhook is set to "all" directions? This run
   observed none.
2. How many webhooks are configured on the Test account? This decides whether `webhook_success` counts
   successful deliveries across webhooks.
3. Is the webhook URL still set to the stopped tunnel?
4. A run with at least 20 inbound transfers per gateway is still needed before any connection can use
   `auto_settle`.
