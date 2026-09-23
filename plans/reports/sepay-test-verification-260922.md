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
| b | Endpoint answered 503 for 15 min 9 s while a 10 006 VND transfer arrived | **PASS** | 2 attempts, 62.5 s apart, with a refreshed `X-SePay-Timestamp`. No retry followed in the next 14 h, so the delivery was lost. |
| c | One outgoing transfer of 10 007 VND | **INCONCLUSIVE** | The API gives `transfer_type` `out` with only `amount_out`. No webhook was sent, so the webhook value for outgoing money was not observed. |
| d | Code-only filter on, a no-code 10 010 VND transfer | **PASS** | No webhook arrived, yet `webhook_success` was 1 right away and still 1 about 14.8 h later. |
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
- The 503 window ran from 10:59:24 to 11:14:33 UTC. After attempt 2 there was no attempt for the
  remaining 9 minutes of the window, none after the endpoint answered 200 again, and none up to capture
  stop at 02:17:25 UTC the next day (14 h later). The transfer was never delivered.
- The SePay documentation read earlier describes one attempt plus seven Fibonacci retries over about
  33 minutes. With 503 answers, this endpoint saw two attempts within about one minute and nothing
  afterwards.
- `recommended_tolerance_seconds` is 300, the phase rule for `refreshed` timestamps. See the skew section
  for why the tolerance must not go below about 160 s.

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
| Filtered by the code-only rule, never sent (10 010) | 1 (right away and about 14.8 h later) |
| Two attempts, both answered 503, never delivered (10 006) | 1 |
| Delivered and answered 200 (10 001 to 10 005, 10 009) | 2 |
| Delivered and answered 200, VA transfer (10 008) | 1 |
| Outgoing, no webhook (10 007) | 0 |

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
and −151.8 s (mean −152.5 s). The local clock was checked against NTP with an offset under 0.1 s.
`transactionDate` agrees with the local clock to within a few seconds. The skew is therefore systematic
on SePay's side: the signature timestamp lags real time by about 2.5 minutes, on first attempts and
retries alike.

## What this means for the package

- **Timestamp tolerance.** Keep `timestamp_tolerance_seconds` at the 300 s default. The header lags by
  about 152 s, so 300 s leaves about 146 s of headroom. Any tolerance under about 160 s would reject
  valid SePay deliveries, which makes the configured minimum of 60 s unsafe for SePay. Retries carry a
  fresh timestamp, so a late retry never needs a wider window.
- **auto_settle stays off.** The verdict's gateway entry has 5 equal pairs and `auto_settle_eligible`
  false, and scenario (a) is INCONCLUSIVE. `FileEvidenceVerifier` rejects this artifact
  (`SCENARIO_A_NOT_PASS`, and `GATEWAY_NOT_ELIGIBLE` even if (a) were forced to PASS). Every connection
  stays `detect_only` until a run with at least 20 equal pairs per gateway.
- **`webhook_success` is unusable as a delivery signal.** Reconciliation must not read it as "the host
  received this transfer". The API reader has to compare API rows with stored observations itself.
- **Retry window and recovery.** Answering 503 cost the (b) transfer for good after about one minute.
  Any outage or 5xx answer that lasts longer than about a minute should be treated as a loss of webhook
  deliveries. API reconciliation is the only recovery path, so its schedule and lookback must cover
  whole outages, not the 33-minute retry schedule described in the documentation.
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
