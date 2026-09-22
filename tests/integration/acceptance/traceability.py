"""Which tests prove each acceptance case (validation-and-acceptance.md) and each invariant
(architecture-design.md, "Bất biến chính"). ``test_traceability.py`` checks every node id
still names a test, and that ``CASES`` has exactly the cases of the document's table.

That check is structural: it proves the mapped tests exist, not that they assert what the
case or invariant states. Whether a mapped test covers its row is decided by reading it.

Invariant 6 (intent immutable): the mapped tests read an intent's merchant, receiving
account and amount back before and after every lifecycle write (cancel, supersede, expire,
paid, late paid through review) and after a refused idempotent replay.
"""

from __future__ import annotations

A = "tests/integration/application"
S = "tests/integration/storage"
U = "tests/unit/domain"
C = "tests/contract/sepay"
ACC = "tests/integration/acceptance"
IMM = f"{A}/test_intent_immutable_fields.py"

CASES: dict[str, tuple[str, ...]] = {
    "Reuse": (
        f"{ACC}/test_reuse_two_hosts.py::test_the_wheel_holds_only_the_package",
        f"{ACC}/test_reuse_two_hosts.py::test_both_hosts_install_the_same_package",
        f"{ACC}/test_reuse_two_hosts.py::test_both_hosts_settle_on_their_own_database",
    ),
    "Merchant isolation": (
        f"{S}/test_tenant_isolation_constraints.py::test_same_account_for_other_tenant_rejected",
        f"{S}/test_tenant_isolation_constraints.py::test_settlement_across_tenants_rejected",
        f"{S}/test_tenant_isolation_constraints.py::test_test_fact_settling_live_intent_rejected",
        f"{A}/test_process_inbox.py::test_reference_of_another_tenant_is_a_tenant_mismatch",
        f"{A}/test_process_inbox.py::test_reference_of_another_environment_is_a_tenant_mismatch",
        f"{A}/test_process_inbox.py::test_receiver_of_another_tenant_is_never_linked",
        f"{A}/test_metrics_best_effort.py::test_tenant_mismatch_is_counted_once_across_a_retry",
        f"{A}/test_ingest_webhook.py::test_unknown_and_disabled_locators_look_the_same",
        f"{ACC}/test_webhook_router_contract.py::"
        "test_unknown_and_disabled_locators_answer_the_same_404",
    ),
    "Create idempotency": (
        f"{A}/test_create_intent.py::test_same_request_is_idempotent",
        f"{A}/test_create_intent.py::test_same_key_with_another_request_conflicts",
        f"{A}/test_create_intent.py::"
        "test_concurrent_different_request_same_key_one_wins_one_conflicts",
        f"{A}/test_create_intent.py::"
        "test_concurrent_same_request_in_host_transactions_yields_one_intent",
        f"{A}/test_create_intent.py::test_replay_with_another_payload_after_expiry_conflicts",
        f"{S}/test_unit_of_work.py::test_same_idempotency_key_and_request_returns_existing_intent",
        f"{S}/test_unit_of_work.py::test_same_idempotency_key_other_request_conflicts",
        f"{ACC}/test_reuse_two_hosts.py::test_both_hosts_settle_on_their_own_database",
    ),
    "Concurrent webhook replay": (
        f"{A}/test_concurrent_replay.py::test_one_hundred_concurrent_replays_settle_once",
        f"{A}/test_concurrent_replay.py::test_inline_processing_racing_the_worker_settles_once",
        f"{ACC}/test_concurrent_replay_http.py::test_one_hundred_concurrent_posts_settle_once",
    ),
    "API + webhook": (
        f"{A}/test_reconcile_linking.py::test_webhook_x3_reconcile_x2_single_settlement",
        f"{A}/test_reconcile_linking.py::test_webhook_then_api_links_to_the_webhook_fact",
        f"{A}/test_reconcile_linking.py::"
        "test_api_then_webhook_in_detect_only_reviews_then_settles_one_fact",
        f"{A}/test_reconcile_linking.py::test_api_then_webhook_in_auto_settle_settles_once",
        f"{A}/test_purge.py::test_settled_fact_keeps_its_memo_while_the_other_source_can_still_link",
    ),
    "Durable ACK": (
        f"{A}/test_ingest_webhook.py::test_no_ack_without_commit",
        f"{ACC}/test_durable_ack_http.py::"
        "test_failed_commit_answers_500_and_the_retry_is_stored_once",
        f"{ACC}/test_durable_ack_http.py::test_unreachable_database_answers_500",
    ),
    "Crash recovery": (
        f"{A}/test_dispatch_outbox.py::test_crash_between_commit_and_publish_replays_the_same_event",
        f"{A}/test_handler_failure_rollback.py::test_crash_after_claim_recovers_after_lease_expiry",
        f"{A}/test_handler_failure_rollback.py::"
        "test_crash_between_rollback_and_failure_update_recovers",
        f"{ACC}/test_fnb_async_host.py::"
        "test_dispatcher_killed_after_the_consumer_committed_redelivers_harmlessly",
    ),
    "Mismatch/late/ambiguous": (
        f"{A}/test_process_inbox.py::test_wrong_amount_opens_review",
        f"{A}/test_process_inbox.py::test_late_payment_opens_review",
        f"{A}/test_process_inbox.py::test_two_references_are_ambiguous",
        f"{A}/test_process_inbox.py::test_no_reference_opens_review",
        f"{A}/test_resolve_review.py::test_no_resolution_settles_mismatched_amount",
        f"{U}/test_match_transaction.py::test_amount_mismatch_goes_to_review",
        f"{U}/test_match_transaction.py::test_late_webhook_goes_to_review",
        f"{U}/test_match_transaction.py::test_two_intents_in_memo_are_ambiguous",
    ),
    "Receiver spoof/outgoing": (
        f"{A}/test_process_inbox.py::test_outgoing_or_unknown_money_is_not_applicable",
        f"{A}/test_process_inbox.py::test_account_not_bound_to_the_connection_is_unbound",
        f"{A}/test_process_inbox.py::test_receiver_of_another_merchant_is_recorded_but_unbound",
        f"{A}/test_process_inbox.py::test_unregistered_receiver_is_recorded_unbound",
        f"{A}/test_rematch.py::test_receiver_spoof_binding_to_another_merchant_is_refused",
        f"{U}/test_invariant_guard.py::test_outgoing_or_unknown_is_checked_before_receiver",
    ),
    "Reference uniqueness": (
        f"{S}/test_unit_of_work.py::test_reference_collision_retries_inside_host_transaction",
        f"{S}/test_unit_of_work.py::test_reference_space_exhausted_after_bounded_retries",
        f"{S}/test_unit_of_work.py::test_reference_lookup_is_project_wide_locked_and_ordered",
        f"{S}/test_money_constraints.py::test_duplicate_payment_reference_rejected",
        f"{A}/test_create_intent.py::test_joined_reference_collision_retries_without_aborting_the_host",
    ),
    "Prefix/provider config": (
        f"{U}/test_reference_profile.py::test_prefix_value_is_two_to_five_ascii_capitals",
        f"{U}/test_reference_profile.py::test_nested_prefix_rejected",
        f"{U}/test_reference_profile.py::test_duplicate_prefix_value_rejected",
        f"{U}/test_reference_profile.py::test_same_prefix_across_versions_allowed_with_advisory",
        f"{U}/test_reference_profile.py::test_suffix_length_bounds",
        f"{U}/test_reference_profile.py::test_alphabet_is_shared_subset_of_capitals_and_digits",
        f"{C}/test_template_checklist.py::test_three_named_prefixes_give_one_template_and_one_filter_each",
        f"{C}/test_template_checklist.py::test_suffix_range_covers_every_accepted_version_of_a_prefix",
        f"{S}/test_money_constraints.py::test_prefix_length_rejected",
        f"{S}/test_money_constraints.py::test_duplicate_prefix_in_version_rejected",
        f"{S}/test_money_constraints.py::test_same_prefix_in_two_versions_accepted",
        f"{S}/test_money_constraints.py::test_intent_with_unknown_prefix_name_rejected",
        f"{S}/test_money_constraints.py::test_same_idempotency_key_in_other_environment_accepted",
        f"{A}/test_reference_profiles.py::test_nested_prefix_in_version_rejected",
        f"{A}/test_reference_profiles.py::test_readiness_requires_every_named_prefix",
        f"{A}/test_create_intent.py::test_unknown_or_missing_prefix_name_is_rejected",
        f"{A}/test_readiness.py::test_environment_must_be_the_connection_environment",
    ),
    "Rotation": (
        f"{A}/test_reference_profiles.py::test_legacy_code_still_matches_after_rotation",
        f"{A}/test_reference_profiles.py::test_same_prefix_new_suffix_length_allowed",
        f"{A}/test_reference_profiles.py::test_retired_version_leaves_the_checklist",
        f"{A}/test_rematch.py::test_rematch_reviews_settles_after_legacy_import",
        f"{A}/test_create_intent.py::test_legacy_reference_is_imported",
        f"{ACC}/test_reuse_two_hosts.py::test_both_hosts_settle_on_their_own_database",
    ),
    "Host async failure": (
        f"{A}/test_dispatch_outbox.py::test_publish_failure_backs_off_then_fails_and_can_be_requeued",
        f"{ACC}/test_fnb_async_host.py::"
        "test_consumer_failure_keeps_the_intent_paid_and_the_retry_pays_the_bill_once",
        f"{ACC}/test_fnb_async_host.py::test_the_consumer_ignores_a_repeated_event",
        f"{ACC}/test_fnb_async_host.py::"
        "test_event_for_a_missing_bill_is_retried_until_the_bill_exists",
        f"{ACC}/test_saas_order_handler.py::"
        "test_settlement_for_a_missing_order_rolls_back_and_is_retried",
    ),
    "Migration/restore": (
        f"{S}/test_schema_parity.py::test_define_tables_and_schema_v1_are_identical",
        f"{S}/test_schema_parity.py::test_downgrade_drops_every_table",
        f"{S}/test_schema_parity.py::test_every_foreign_key_targets_an_exact_unique_key",
        f"{ACC}/test_migration_restore.py::test_dump_and_restore_keeps_money_and_constraints",
    ),
}

# Rows the plan adds to the document's table.
PLAN_CASES: dict[str, tuple[str, ...]] = {
    "Same-tenant ownership": (
        f"{S}/test_tenant_isolation_constraints.py::"
        "test_binding_account_of_other_merchant_same_tenant_rejected",
        f"{S}/test_tenant_isolation_constraints.py::test_binding_claiming_other_merchant_rejected",
        f"{S}/test_tenant_isolation_constraints.py::test_binding_live_account_to_test_connection_rejected",
        f"{S}/test_tenant_isolation_constraints.py::"
        "test_fact_resolved_to_account_of_other_merchant_rejected",
        f"{S}/test_tenant_isolation_constraints.py::"
        "test_same_account_for_other_merchant_of_same_tenant_rejected",
        f"{S}/test_tenant_isolation_constraints.py::test_same_account_in_test_and_live_accepted",
        f"{A}/test_onboarding.py::test_one_owner_per_account_and_environment",
        f"{A}/test_onboarding.py::test_bind_across_merchant_or_environment_is_refused",
        f"{A}/test_onboarding.py::test_database_refuses_a_cross_merchant_binding",
        f"{A}/test_create_intent.py::test_account_of_another_merchant_is_rejected",
        f"{U}/test_match_transaction.py::test_same_tenant_other_account_not_settled",
        f"{U}/test_match_transaction.py::test_test_intent_live_fact_not_settled",
    ),
    "Reconciliation safety": (
        f"{A}/test_reconcile_linking.py::test_concurrent_webhook_and_api_make_one_fact",
        f"{A}/test_reconcile_linking.py::"
        "test_distinct_transfers_with_the_same_reference_are_never_merged",
        f"{A}/test_reconcile_linking.py::"
        "test_two_api_only_transfers_with_the_same_reference_stay_separate",
        f"{A}/test_reconcile_linking.py::test_api_id_already_on_another_fact_is_never_overwritten",
        f"{A}/test_reconcile_linking.py::"
        "test_ambiguous_sighting_gets_its_own_fact_and_is_never_settled",
        f"{A}/test_reconcile_modes.py::"
        "test_unlinked_observation_is_decided_after_grace_though_the_cursor_moved",
        f"{A}/test_reconcile_modes.py::test_failed_read_keeps_the_checkpoint",
        f"{S}/test_claim_skip_locked.py::test_expired_inbox_lease_is_reclaimed_with_new_generation",
        f"{S}/test_claim_skip_locked.py::test_stale_inbox_owner_cannot_finalize_after_reclaim",
        f"{S}/test_claim_skip_locked.py::"
        "test_expired_outbox_lease_is_recovered_and_old_owner_fenced",
        f"{A}/test_handler_failure_rollback.py::test_old_owner_cannot_finalize_after_reclaim",
        f"{A}/test_purge.py::test_purged_delivery_is_never_claimed",
        f"{A}/test_purge.py::test_purged_failed_delivery_cannot_be_requeued",
    ),
}

INVARIANTS: dict[int, tuple[str, ...]] = {
    1: (
        f"{C}/test_provider_verify.py::test_valid_signature_verifies_raw_bytes",
        f"{C}/test_provider_verify.py::test_body_changed_after_signing_is_invalid",
        f"{C}/test_provider_verify.py::test_signature_binds_the_timestamp",
        f"{A}/test_ingest_webhook.py::test_wrong_signature_is_rejected_and_not_stored",
        f"{A}/test_ingest_webhook.py::test_unknown_and_disabled_locators_look_the_same",
        f"{ACC}/test_webhook_router_contract.py::"
        "test_unknown_and_disabled_locators_answer_the_same_404",
        f"{ACC}/test_webhook_router_contract.py::"
        "test_signature_or_timestamp_failure_is_401_and_stores_nothing",
    ),
    2: (
        f"{U}/test_invariant_guard.py::test_outgoing_or_unknown_is_checked_before_receiver",
        f"{U}/test_match_transaction.py::test_policy_cannot_widen_amount",
        f"{U}/test_match_transaction.py::test_policy_cannot_widen_lateness",
        f"{U}/test_match_transaction.py::test_policy_cannot_widen_with_core_reason",
        f"{U}/test_match_transaction.py::test_other_tenant_intent_review_without_candidate",
        f"{U}/test_match_transaction.py::test_review_reason_has_exactly_ten_values",
        f"{A}/test_process_inbox.py::test_reference_of_another_tenant_is_a_tenant_mismatch",
    ),
    3: (
        f"{A}/test_ingest_webhook.py::test_no_ack_without_commit",
        f"{ACC}/test_durable_ack_http.py::"
        "test_failed_commit_answers_500_and_the_retry_is_stored_once",
        f"{ACC}/test_webhook_router_contract.py::test_every_stored_delivery_is_acknowledged",
    ),
    4: (
        f"{S}/test_tenant_isolation_constraints.py::test_duplicate_event_key_rejected",
        f"{S}/test_tenant_isolation_constraints.py::test_same_dedup_key_twice_in_scope_rejected",
        f"{S}/test_money_constraints.py::test_two_settlements_for_one_fact_rejected",
        f"{S}/test_money_constraints.py::test_two_settlements_for_one_intent_rejected",
        f"{ACC}/test_concurrent_replay_http.py::test_one_hundred_concurrent_posts_settle_once",
    ),
    5: (
        f"{A}/test_process_inbox.py::test_unregistered_receiver_is_recorded_unbound",
        f"{A}/test_process_inbox.py::test_no_reference_opens_review",
        f"{A}/test_rematch.py::test_unbound_money_settles_after_bind_and_rematch",
        f"{A}/test_purge.py::test_free_text_of_a_fact_in_review_is_kept_until_it_is_decided",
        f"{A}/test_purge.py::test_settled_fact_keeps_its_memo_while_the_other_source_can_still_link",
        f"{A}/test_purge.py::test_fact_with_a_recent_sighting_waits_for_the_link_horizon",
    ),
    6: (
        f"{IMM}::test_cancel_keeps_merchant_account_and_amount",
        f"{IMM}::test_supersede_keeps_both_intents_unchanged",
        f"{IMM}::test_expire_keeps_merchant_account_and_amount",
        f"{IMM}::test_payment_keeps_merchant_account_and_amount",
        f"{IMM}::test_late_payment_accepted_in_review_keeps_merchant_account_and_amount",
        f"{IMM}::test_refused_replay_keeps_the_stored_request",
        f"{IMM}::test_intent_expiry_is_reported_without_a_write",
        f"{U}/test_intent_transitions.py::test_every_other_intent_transition_is_illegal",
    ),
    7: (
        f"{U}/test_exact_amount_policy.py::test_exact_amount_on_time_settles",
        f"{U}/test_exact_amount_policy.py::test_amount_mismatch_wins_over_late",
        f"{A}/test_process_inbox.py::test_wrong_amount_opens_review",
        f"{S}/test_money_constraints.py::test_auto_amount_mismatch_rejected",
    ),
    8: (
        f"{A}/test_apply_match_outcome.py::test_observer_failure_rolls_back_the_settlement",
        f"{A}/test_handler_failure_rollback.py::test_handler_failure_rolls_back_and_retries",
        f"{A}/test_dispatch_outbox.py::test_pending_event_is_published_once",
        f"{ACC}/test_reuse_two_hosts.py::test_both_hosts_settle_on_their_own_database",
        f"{ACC}/test_saas_order_handler.py::"
        "test_settlement_for_a_missing_order_rolls_back_and_is_retried",
    ),
    9: (
        f"{A}/test_dispatch_outbox.py::test_publish_failure_backs_off_then_fails_and_can_be_requeued",
        f"{ACC}/test_fnb_async_host.py::"
        "test_consumer_failure_keeps_the_intent_paid_and_the_retry_pays_the_bill_once",
        f"{ACC}/test_fnb_async_host.py::"
        "test_event_for_a_missing_bill_is_retried_until_the_bill_exists",
    ),
    10: (
        f"{A}/test_create_intent.py::test_creates_intent_with_instruction_and_snapshot",
        f"{A}/test_create_intent.py::test_status_reports_expired_without_writing",
        f"{ACC}/test_reuse_two_hosts.py::test_both_hosts_settle_on_their_own_database",
    ),
    11: (
        f"{U}/test_match_transaction.py::test_same_prefix_other_code_is_not_a_match",
        f"{U}/test_reference_resolver.py::test_same_prefix_other_reference_does_not_match",
        f"{A}/test_process_inbox.py::test_receiver_of_another_tenant_is_never_linked",
    ),
}
