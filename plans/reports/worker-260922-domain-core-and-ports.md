# Worker report: domain core and ports

Date: 22/09/2026. Phase file: `plans/260921-1138-payment-module-package-v1/phase-02-domain-core-and-ports.md`. Its Status is now done and every Todo item is ticked.

## Outcome

The pure domain, the fixed matching chain and the port Protocols are in place under `src/payment_module/{domain,ports,reference}`. There are 269 new unit tests, and none of them needs Docker. All Success Criteria commands pass. The coordinator confirmed that the phase-02 addendum in `plans/reports/kongming-260922-0051-phase-01-gate.md` §3 is binding, and I implemented it.

## What changed

- **Domain (`domain/`):**
  - `money.AmountVnd`: rejects `bool`, accepts a float only when it is a whole number, and accepts strings of ASCII digits only.
  - `account_identity.account_key`: builds `BANK|ACCOUNT|SUB`.
  - `reference`:
    - `NamedPrefix` requires a name matching `[a-z][a-z0-9_]{0,31}` and a prefix of 2 to 5 capital letters `A–Z`.
    - `ReferenceProfile` supports `generated` and `legacy_import` profiles. All prefixes in a version share one suffix length and alphabet. It has `prefix_for` and `shapes()`.
    - `check_prefix_overlap` treats overlaps inside a version as errors and overlaps across versions as advisories.
    - `PaymentReference` and `tokens_from` implement the whole-token normalization contract.
  - `enums`: StrEnums that use the exact database strings, including every enum the addendum requires. `ReviewReason` has exactly 10 values.
  - `intent` and `transaction`: views plus pure transition functions.
  - `review`: `MatchDecision` and the `Settle`, `Review` and `NotApplicable` outcomes.
  - `events`: v1 of `PaymentSettled`, `PaymentNeedsReview` and `ReviewResolved`. Each carries `trusted_scope`, and the canonical JSON is covered by a snapshot test.
  - `errors`: the domain error types.
- **Matching (`domain/matching/`):** `InvariantGuard` checks direction first, then receiver and merchant scope. After that come `ReferenceResolver` (whole-token match, then the scope check), `IntentEligibility`, `MatchingPolicy`/`ExactAmountPolicy` (amount before lateness), and `MatchTransaction` with the pure `ensure_settle_allowed(..., allow_late=False)` post-check.
- **Ports (`ports/`):**
  - Protocols and DTOs for the provider, reader, connection and secret resolvers, unit of work, reference generator, template checklist, handlers, outbox publisher and clock.
  - The unit of work declares minimal repository Protocols. The only methods are `get_for_update`, `insert_or_get_by_dedup_key` and the two `claim_batch` methods.
  - Anything that touches I/O is async, because phase 04 uses an async SQLAlchemy UoW.
- **`reference/random_suffix_generator.py`:** the prefix plus a suffix drawn with `secrets.choice` from the profile alphabet. The package ships no default alphabet constant; the alphabet is profile configuration (N1).
- **Tests (`tests/unit/domain/`):** the 11 planned files, plus `test_events.py`, `test_account_identity.py` and `conftest.py`.

## Commands run

| Command | Result |
|---|---|
| `uv run pytest tests/unit -q` | 270 passed |
| `uv run python -c "…ReviewReason… len(R) == 10 and R.TENANT_MISMATCH"` | exit 0 |
| `uv run pytest tests/unit/domain/test_match_transaction.py -q -k policy_cannot_widen` | 11 passed |
| `uv run pytest tests/unit/domain/test_layer_imports.py -q` | 7 passed |
| `uv run pytest tests/unit/domain/test_reference_profile.py -q -k "nested_prefix_rejected or unknown_prefix_name or same_prefix_across_versions_allowed"` | 4 passed |
| `uv run ruff check . && uv run ruff format --check . && uv run pytest -q` | clean; 95 files formatted; 270 passed |

## Deviations and why

- **Environment mismatch opens a review with `candidate_intent_id = NULL`, not `intent.id`.**
  - The addendum and the coordinator message say to set a candidate whenever the tenant matches.
  - The concurrent reconciled `data-model.md` (O16 and its scope table) sets NULL for the `environment` scope, because the review→intent FK carries `environment`. The database would reject a candidate from the other environment.
  - I followed the data model. Only the `receiving_account` scope carries `intent.id`.
- **Scope mismatch between a fact and its own connection yields `RECEIVER_UNBOUND`.** This covers a tenant, environment or merchant that differs from the connection. The guard has only three results, and this outcome never reaches the policy.
- **`legacy_import` profiles also accept `retired`,** so that `RetireReferenceProfile` can end them later. They still never generate codes.
- **The alphabet must not repeat characters.** A repeated character would bias the random draw.
- **Files added outside the list:**
  - `__init__.py` for the three new packages.
  - `tests/unit/domain/conftest.py` with shared builders.
  - `test_events.py`, which holds the event snapshot that the Success Criteria require.
  - `test_account_identity.py`.
- **`ReferenceTemplateChecklist.checklist` has an extra parameter, `accepted: Sequence[PrefixShape] = ()`.** Phase 07 needs the other accepted versions to compute suffix min/max.
- **`CrossVersionOverlap` carries the candidate `NamedPrefix` and the accepted `PrefixShape`.** The candidate's suffix length and alphabet come from the candidate profile itself.

## Open questions

- Should the environment scope carry a candidate intent after all? If so, the review FK in phase 04 must change. For now the code follows `data-model.md` O16.
