"""Frozen DDL for payment module schema version 1.

Call ``upgrade(op, prefix)`` from the host's Alembic revision to create the tables on an
existing database, and ``downgrade(op, prefix)`` to drop them. A host that starts from an
empty database may instead run ``metadata.create_all`` on ``define_tables`` and stamp its
revision; the package parity test proves both produce the same schema on PostgreSQL.

This file is written by hand and never generated from the live table definitions: value
lists, names and types below are the v1 contract and must not change. A later schema
version adds its own module (``schema_v2.upgrade_from_v1``) instead of editing this one.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

SCHEMA_VERSION = 1

_ENVIRONMENT = "environment IN ('test', 'live')"
_DIRECTION = "direction IN ('in', 'out', 'unknown')"


def _json() -> sa.types.TypeEngine[Any]:
    return sa.JSON().with_variant(JSONB(), "postgresql")


def _ts(name: str, *, nullable: bool = False, server_now: bool = False) -> sa.Column[Any]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.func.now() if server_now else None,
    )


def _str(name: str, length: int, *, nullable: bool = False, **kwargs: Any) -> sa.Column[Any]:
    return sa.Column(name, sa.String(length), nullable=nullable, **kwargs)


def _uuid(name: str, *, nullable: bool = False, primary_key: bool = False) -> sa.Column[Any]:
    return sa.Column(name, sa.Uuid(), nullable=nullable, primary_key=primary_key)


def _tenant() -> sa.Column[Any]:
    return _str("tenant_id", 128)


def _environment() -> sa.Column[Any]:
    return _str("environment", 16)


def _fk(name: str, columns: list[str], table: str, refcolumns: list[str]) -> Any:
    return sa.ForeignKeyConstraint(columns, [f"{table}.{c}" for c in refcolumns], name=name)


def upgrade(op: Any, prefix: str = "pm_") -> None:
    """Create the 15 schema-v1 tables with every constraint and index."""

    def n(name: str) -> str:
        return f"{prefix}{name}"

    merchants = n("merchants")
    accounts = n("receiving_accounts")
    connections = n("provider_connections")
    profiles = n("reference_profiles")
    prefixes = n("reference_profile_prefixes")
    intents = n("payment_intents")
    inbox = n("webhook_inbox")
    runs = n("reconciliation_runs")
    transactions = n("provider_transactions")
    review_cases = n("review_cases")

    op.create_table(
        profiles,
        sa.Column("version", sa.Integer(), primary_key=True, autoincrement=False),
        _str("kind", 32),
        sa.Column("suffix_length", sa.Integer(), nullable=True),
        _str("alphabet", 64, nullable=True),
        _str("status", 32),
        _ts("created_at", server_now=True),
        _ts("activated_at", nullable=True),
        _str("activated_by", 255, nullable=True),
        _ts("retired_at", nullable=True),
        _str("retired_by", 255, nullable=True),
        sa.CheckConstraint("kind IN ('generated', 'legacy_import')", name=n("profiles_kind_ck")),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'accepted_legacy', 'retired')",
            name=n("profiles_status_ck"),
        ),
        sa.CheckConstraint(
            "kind = 'legacy_import' OR (suffix_length BETWEEN 1 AND 30 AND alphabet IS NOT NULL)",
            name=n("profiles_generated_shape_ck"),
        ),
    )
    op.create_index(
        n("profiles_one_active_uq"),
        profiles,
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        prefixes,
        sa.Column("profile_version", sa.Integer(), primary_key=True, autoincrement=False),
        _str("name", 64, primary_key=True),
        _str("prefix", 16),
        _ts("created_at", server_now=True),
        sa.UniqueConstraint("profile_version", "prefix", name=n("prefixes_version_prefix_uq")),
        _fk(n("prefixes_profile_fk"), ["profile_version"], profiles, ["version"]),
        sa.CheckConstraint("length(prefix) BETWEEN 2 AND 5", name=n("prefixes_prefix_len_ck")),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 32", name=n("prefixes_name_len_ck")),
    )

    op.create_table(
        merchants,
        _uuid("id", primary_key=True),
        _tenant(),
        _str("host_merchant_ref", 255),
        _str("status", 32),
        _ts("created_at", server_now=True),
        sa.UniqueConstraint("tenant_id", "id", name=n("merchants_tenant_id_uq")),
        sa.UniqueConstraint(
            "tenant_id", "host_merchant_ref", name=n("merchants_tenant_host_ref_uq")
        ),
        sa.CheckConstraint("status IN ('active', 'disabled')", name=n("merchants_status_ck")),
    )

    op.create_table(
        accounts,
        _uuid("id", primary_key=True),
        _tenant(),
        _uuid("merchant_id"),
        _environment(),
        _str("bank_code", 32),
        _str("bank_bin", 16, nullable=True),
        _str("account_number", 64),
        _str("sub_account", 64, server_default=""),
        _str("account_number_masked", 64),
        _str("holder_name", 255),
        _str("account_fingerprint", 255),
        _str("provider_account_ref", 255, nullable=True),
        _str("status", 32),
        sa.UniqueConstraint(
            "environment", "account_fingerprint", name=n("accounts_env_fingerprint_uq")
        ),
        sa.UniqueConstraint(
            "tenant_id", "merchant_id", "environment", "id", name=n("accounts_scope_id_uq")
        ),
        _fk(
            n("accounts_merchant_fk"), ["tenant_id", "merchant_id"], merchants, ["tenant_id", "id"]
        ),
        sa.CheckConstraint(_ENVIRONMENT, name=n("accounts_environment_ck")),
        sa.CheckConstraint(
            "status IN ('active', 'disabled', 'retired')", name=n("accounts_status_ck")
        ),
    )

    op.create_table(
        connections,
        _uuid("id", primary_key=True),
        _tenant(),
        _uuid("merchant_id"),
        _str("provider", 32),
        _environment(),
        _str("locator", 128),
        _str("secret_ref", 255),
        _str("api_credential_ref", 255, nullable=True),
        _str("auth_mode", 32),
        _str("reconcile_mode", 32, server_default="detect_only"),
        _str("reconcile_evidence_ref", 255, nullable=True),
        sa.Column(
            "timestamp_tolerance_seconds", sa.Integer(), nullable=False, server_default="300"
        ),
        _str("status", 32, server_default="pending"),
        _str("status_changed_by", 255, nullable=True),
        _ts("status_changed_at", nullable=True),
        sa.Column("status_changed_reason", sa.Text(), nullable=True),
        sa.UniqueConstraint("locator", name=n("connections_locator_uq")),
        sa.UniqueConstraint(
            "tenant_id", "merchant_id", "environment", "id", name=n("connections_scope_id_uq")
        ),
        sa.UniqueConstraint(
            "tenant_id", "environment", "id", name=n("connections_tenant_env_id_uq")
        ),
        sa.UniqueConstraint("tenant_id", "id", name=n("connections_tenant_id_uq")),
        _fk(
            n("connections_merchant_fk"),
            ["tenant_id", "merchant_id"],
            merchants,
            ["tenant_id", "id"],
        ),
        sa.CheckConstraint(_ENVIRONMENT, name=n("connections_environment_ck")),
        sa.CheckConstraint("auth_mode IN ('hmac')", name=n("connections_auth_mode_ck")),
        sa.CheckConstraint(
            "reconcile_mode IN ('detect_only', 'auto_settle')",
            name=n("connections_reconcile_mode_ck"),
        ),
        sa.CheckConstraint(
            "reconcile_mode = 'detect_only' OR reconcile_evidence_ref IS NOT NULL",
            name=n("connections_auto_settle_evidence_ck"),
        ),
        sa.CheckConstraint(
            "timestamp_tolerance_seconds BETWEEN 60 AND 7200", name=n("connections_tolerance_ck")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'not_ready', 'disabled')",
            name=n("connections_status_ck"),
        ),
    )

    op.create_table(
        n("connection_account_bindings"),
        _tenant(),
        _uuid("merchant_id"),
        _environment(),
        _uuid("connection_id", primary_key=True),
        _uuid("receiving_account_id", primary_key=True),
        _str("created_by", 255),
        _ts("created_at", server_now=True),
        _fk(
            n("bindings_connection_fk"),
            ["tenant_id", "merchant_id", "environment", "connection_id"],
            connections,
            ["tenant_id", "merchant_id", "environment", "id"],
        ),
        _fk(
            n("bindings_account_fk"),
            ["tenant_id", "merchant_id", "environment", "receiving_account_id"],
            accounts,
            ["tenant_id", "merchant_id", "environment", "id"],
        ),
        sa.CheckConstraint(_ENVIRONMENT, name=n("bindings_environment_ck")),
    )

    op.create_table(
        n("connection_reference_readiness"),
        _uuid("id", primary_key=True),
        _tenant(),
        _uuid("connection_id"),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        _environment(),
        _str("status", 32),
        sa.Column("checklist", _json(), nullable=False),
        _str("evidence_ref", 255, nullable=True),
        _str("verified_by", 255, nullable=True),
        _ts("verified_at", nullable=True),
        sa.UniqueConstraint(
            "connection_id", "profile_version", "environment", name=n("readiness_key_uq")
        ),
        _fk(
            n("readiness_connection_fk"),
            ["tenant_id", "environment", "connection_id"],
            connections,
            ["tenant_id", "environment", "id"],
        ),
        _fk(n("readiness_profile_fk"), ["profile_version"], profiles, ["version"]),
        sa.CheckConstraint(_ENVIRONMENT, name=n("readiness_environment_ck")),
        sa.CheckConstraint(
            "status IN ('pending', 'ready', 'retired')", name=n("readiness_status_ck")
        ),
    )

    op.create_table(
        intents,
        _uuid("id", primary_key=True),
        _tenant(),
        _uuid("merchant_id"),
        _environment(),
        _uuid("receiving_account_id"),
        sa.Column("amount_vnd", sa.BigInteger(), nullable=False),
        _str("currency", 3, server_default="VND"),
        sa.Column("beneficiary_snapshot", _json(), nullable=False),
        _str("payment_reference", 64),
        sa.Column("reference_profile_version", sa.Integer(), nullable=False),
        _str("reference_prefix_name", 64, nullable=True),
        _str("host_ref_type", 64),
        _str("host_ref_id", 255),
        _str("idempotency_key", 255),
        _str("request_fingerprint", 128),
        _ts("expires_at"),
        _str("status", 32),
        _uuid("superseded_by_intent_id", nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        _ts("created_at", server_now=True),
        _ts("paid_at", nullable=True),
        sa.UniqueConstraint("payment_reference", name=n("intents_reference_uq")),
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key", "environment", name=n("intents_idempotency_uq")
        ),
        sa.UniqueConstraint(
            "tenant_id", "merchant_id", "environment", "id", name=n("intents_scope_id_uq")
        ),
        sa.UniqueConstraint("tenant_id", "environment", "id", name=n("intents_tenant_env_id_uq")),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            "amount_vnd",
            "receiving_account_id",
            "environment",
            name=n("intents_settlement_key_uq"),
        ),
        _fk(n("intents_merchant_fk"), ["tenant_id", "merchant_id"], merchants, ["tenant_id", "id"]),
        _fk(
            n("intents_account_fk"),
            ["tenant_id", "merchant_id", "environment", "receiving_account_id"],
            accounts,
            ["tenant_id", "merchant_id", "environment", "id"],
        ),
        _fk(n("intents_profile_fk"), ["reference_profile_version"], profiles, ["version"]),
        _fk(
            n("intents_prefix_fk"),
            ["reference_profile_version", "reference_prefix_name"],
            prefixes,
            ["profile_version", "name"],
        ),
        _fk(
            n("intents_superseded_by_fk"),
            ["tenant_id", "merchant_id", "environment", "superseded_by_intent_id"],
            intents,
            ["tenant_id", "merchant_id", "environment", "id"],
        ),
        sa.CheckConstraint("amount_vnd > 0", name=n("intents_amount_positive_ck")),
        sa.CheckConstraint("currency = 'VND'", name=n("intents_currency_ck")),
        sa.CheckConstraint(_ENVIRONMENT, name=n("intents_environment_ck")),
        sa.CheckConstraint(
            "status IN ('awaiting_payment', 'paid', 'expired', 'cancelled', 'superseded')",
            name=n("intents_status_ck"),
        ),
    )
    op.create_index(
        n("intents_account_reference_ix"), intents, ["receiving_account_id", "payment_reference"]
    )

    op.create_table(
        inbox,
        _uuid("id", primary_key=True),
        _tenant(),
        _uuid("connection_id"),
        _str("event_key", 255),
        _str("event_key_kind", 32),
        _str("body_sha256", 64),
        sa.Column("raw_body", sa.LargeBinary(), nullable=True),
        sa.Column("headers", _json(), nullable=True),
        _ts("received_at"),
        _str("status", 32),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        _ts("next_attempt_at", nullable=True),
        _str("lease_owner", 255, nullable=True),
        _ts("lease_until", nullable=True),
        sa.Column("lease_generation", sa.BigInteger(), nullable=False, server_default="0"),
        _str("last_error_code", 64, nullable=True),
        _ts("purge_after", nullable=True),
        _ts("raw_purged_at", nullable=True),
        sa.UniqueConstraint("connection_id", "event_key", name=n("inbox_event_key_uq")),
        sa.UniqueConstraint("tenant_id", "connection_id", "id", name=n("inbox_scope_id_uq")),
        _fk(
            n("inbox_connection_fk"),
            ["tenant_id", "connection_id"],
            connections,
            ["tenant_id", "id"],
        ),
        sa.CheckConstraint(
            "event_key_kind IN ('provider_id', 'body_hash')", name=n("inbox_event_key_kind_ck")
        ),
        sa.CheckConstraint(
            "status IN ('received', 'processing', 'processed', 'retry_wait', 'failed', "
            "'quarantined')",
            name=n("inbox_status_ck"),
        ),
        sa.CheckConstraint(
            "raw_body IS NOT NULL OR raw_purged_at IS NOT NULL",
            name=n("inbox_body_or_purged_ck"),
        ),
    )
    op.create_index(n("inbox_status_next_attempt_ix"), inbox, ["status", "next_attempt_at"])

    op.create_table(
        runs,
        _uuid("id", primary_key=True),
        _tenant(),
        _uuid("connection_id"),
        _ts("window_from"),
        _ts("window_to"),
        _str("cursor", 255, nullable=True),
        _str("status", 32),
        sa.Column("counts", _json(), nullable=False),
        _ts("started_at"),
        _ts("finished_at", nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.UniqueConstraint("tenant_id", "connection_id", "id", name=n("recon_runs_scope_id_uq")),
        _fk(
            n("recon_runs_connection_fk"),
            ["tenant_id", "connection_id"],
            connections,
            ["tenant_id", "id"],
        ),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed')", name=n("recon_runs_status_ck")
        ),
    )

    op.create_table(
        transactions,
        _uuid("id", primary_key=True),
        _tenant(),
        _uuid("merchant_id", nullable=True),
        _environment(),
        _str("provider", 32),
        _str("provider_account_key", 255),
        _uuid("receiving_account_id", nullable=True),
        _str("identity_kind", 32),
        _str("identity_value", 255),
        _str("dedup_key", 600),
        _str("webhook_tx_id", 255, nullable=True),
        _str("api_tx_id", 255, nullable=True),
        _str("bank_reference", 255, nullable=True),
        sa.Column("amount_vnd", sa.BigInteger(), nullable=False),
        _str("direction", 16),
        _ts("occurred_at", nullable=True),
        sa.Column("memo", sa.Text(), nullable=True),
        _str("first_source", 32),
        _str("match_state", 32),
        _uuid("duplicate_of_transaction_id", nullable=True),
        _ts("created_at", server_now=True),
        _ts("purge_after", nullable=True),
        sa.UniqueConstraint(
            "tenant_id", "environment", "dedup_key", name=n("transactions_dedup_uq")
        ),
        sa.UniqueConstraint(
            "tenant_id", "environment", "id", name=n("transactions_tenant_env_id_uq")
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "environment",
            "provider",
            "provider_account_key",
            "id",
            name=n("transactions_account_scope_id_uq"),
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "id",
            "amount_vnd",
            "receiving_account_id",
            "environment",
            name=n("transactions_settlement_key_uq"),
        ),
        _fk(
            n("transactions_account_fk"),
            ["tenant_id", "merchant_id", "environment", "receiving_account_id"],
            accounts,
            ["tenant_id", "merchant_id", "environment", "id"],
        ),
        _fk(
            n("transactions_duplicate_of_fk"),
            ["tenant_id", "environment", "duplicate_of_transaction_id"],
            transactions,
            ["tenant_id", "environment", "id"],
        ),
        sa.CheckConstraint("amount_vnd >= 0", name=n("transactions_amount_ck")),
        sa.CheckConstraint(
            "(merchant_id IS NULL) = (receiving_account_id IS NULL)",
            name=n("transactions_receiver_pair_ck"),
        ),
        sa.CheckConstraint(
            "match_state <> 'settled' OR (receiving_account_id IS NOT NULL AND direction = 'in')",
            name=n("transactions_settled_shape_ck"),
        ),
        sa.CheckConstraint(_ENVIRONMENT, name=n("transactions_environment_ck")),
        sa.CheckConstraint(
            "identity_kind IN ('webhook_id', 'api_id')", name=n("transactions_identity_kind_ck")
        ),
        sa.CheckConstraint(_DIRECTION, name=n("transactions_direction_ck")),
        sa.CheckConstraint(
            "first_source IN ('webhook', 'reconcile')", name=n("transactions_first_source_ck")
        ),
        sa.CheckConstraint(
            "match_state IN ('recorded', 'settled', 'in_review', 'not_applicable', "
            "'closed_external', 'duplicate_of')",
            name=n("transactions_match_state_ck"),
        ),
    )
    scope = ["tenant_id", "environment", "provider", "provider_account_key"]
    op.create_index(
        n("transactions_webhook_tx_uq"),
        transactions,
        [*scope, "webhook_tx_id"],
        unique=True,
        postgresql_where=sa.text("webhook_tx_id IS NOT NULL"),
    )
    op.create_index(
        n("transactions_api_tx_uq"),
        transactions,
        [*scope, "api_tx_id"],
        unique=True,
        postgresql_where=sa.text("api_tx_id IS NOT NULL"),
    )
    op.create_index(n("transactions_bank_reference_ix"), transactions, ["bank_reference"])

    op.create_table(
        n("provider_observations"),
        _uuid("id", primary_key=True),
        _tenant(),
        _environment(),
        _uuid("connection_id"),
        _str("provider", 32),
        _str("source", 16),
        _str("source_tx_id", 255),
        _uuid("inbox_id", nullable=True),
        _uuid("reconciliation_run_id", nullable=True),
        _str("reported_account_key", 255),
        _str("bank_reference", 255, nullable=True),
        sa.Column("amount_vnd", sa.BigInteger(), nullable=False),
        _str("direction", 16),
        _ts("occurred_at", nullable=True),
        _str("code", 64, nullable=True),
        sa.Column("memo", sa.Text(), nullable=True),
        sa.Column("normalized", _json(), nullable=True),
        _uuid("transaction_id", nullable=True),
        _str("link_method", 32, nullable=True),
        _str("link_status", 32),
        _ts("observed_at"),
        _ts("purge_after", nullable=True),
        sa.UniqueConstraint(
            "provider", "source", "source_tx_id", "connection_id", name=n("observations_source_uq")
        ),
        _fk(
            n("observations_connection_fk"),
            ["tenant_id", "environment", "connection_id"],
            connections,
            ["tenant_id", "environment", "id"],
        ),
        _fk(
            n("observations_inbox_fk"),
            ["tenant_id", "connection_id", "inbox_id"],
            inbox,
            ["tenant_id", "connection_id", "id"],
        ),
        _fk(
            n("observations_recon_run_fk"),
            ["tenant_id", "connection_id", "reconciliation_run_id"],
            runs,
            ["tenant_id", "connection_id", "id"],
        ),
        _fk(
            n("observations_transaction_fk"),
            ["tenant_id", "environment", "provider", "reported_account_key", "transaction_id"],
            transactions,
            ["tenant_id", "environment", "provider", "provider_account_key", "id"],
        ),
        sa.CheckConstraint(
            "(source = 'webhook' AND inbox_id IS NOT NULL) "
            "OR (source = 'api' AND reconciliation_run_id IS NOT NULL)",
            name=n("observations_provenance_ck"),
        ),
        sa.CheckConstraint(_ENVIRONMENT, name=n("observations_environment_ck")),
        sa.CheckConstraint("source IN ('webhook', 'api')", name=n("observations_source_ck")),
        sa.CheckConstraint(_DIRECTION, name=n("observations_direction_ck")),
        sa.CheckConstraint(
            "link_method IN ('same_source_id', 'bank_reference', 'operator')",
            name=n("observations_link_method_ck"),
        ),
        sa.CheckConstraint(
            "link_status IN ('linked', 'unlinked', 'ambiguous')",
            name=n("observations_link_status_ck"),
        ),
    )
    op.create_index(n("observations_link_status_ix"), n("provider_observations"), ["link_status"])

    op.create_table(
        review_cases,
        _uuid("id", primary_key=True),
        _tenant(),
        _environment(),
        _uuid("transaction_id"),
        _uuid("candidate_intent_id", nullable=True),
        _str("reason", 32),
        sa.Column("details", _json(), nullable=False),
        _str("status", 32),
        _str("resolution", 32, nullable=True),
        _str("resolution_ref", 255, nullable=True),
        _str("resolved_by", 255, nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        _ts("opened_at"),
        _ts("resolved_at", nullable=True),
        sa.UniqueConstraint(
            "id", "tenant_id", "transaction_id", name=n("review_cases_settlement_key_uq")
        ),
        _fk(
            n("review_cases_transaction_fk"),
            ["tenant_id", "environment", "transaction_id"],
            transactions,
            ["tenant_id", "environment", "id"],
        ),
        _fk(
            n("review_cases_candidate_fk"),
            ["tenant_id", "environment", "candidate_intent_id"],
            intents,
            ["tenant_id", "environment", "id"],
        ),
        sa.CheckConstraint(_ENVIRONMENT, name=n("review_cases_environment_ck")),
        sa.CheckConstraint(
            "reason IN ('RECEIVER_UNBOUND', 'NO_REFERENCE', 'AMBIGUOUS_REFERENCE', "
            "'TENANT_MISMATCH', 'ALREADY_PAID', 'INTENT_CANCELLED', 'INTENT_SUPERSEDED', "
            "'AMOUNT_MISMATCH', 'LATE', 'UNVERIFIED_IDENTITY')",
            name=n("review_cases_reason_ck"),
        ),
        sa.CheckConstraint("status IN ('open', 'resolved')", name=n("review_cases_status_ck")),
        sa.CheckConstraint(
            "resolution IN ('attach_to_intent', 'mark_external', 'mark_duplicate_of', "
            "'bind_receiver', 'accept_late')",
            name=n("review_cases_resolution_ck"),
        ),
    )
    op.create_index(
        n("review_cases_one_open_uq"),
        review_cases,
        ["transaction_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )

    op.create_table(
        n("settlements"),
        _uuid("id", primary_key=True),
        _tenant(),
        _environment(),
        _uuid("transaction_id"),
        _uuid("intent_id"),
        _uuid("receiving_account_id"),
        sa.Column("amount_vnd", sa.BigInteger(), nullable=False),
        sa.Column("intent_amount_vnd", sa.BigInteger(), nullable=False),
        _str("origin", 32),
        _uuid("review_case_id", nullable=True),
        _str("resolved_by", 255, nullable=True),
        _ts("settled_at"),
        sa.UniqueConstraint("transaction_id", name=n("settlements_transaction_uq")),
        sa.UniqueConstraint("intent_id", name=n("settlements_intent_uq")),
        _fk(
            n("settlements_transaction_fk"),
            ["tenant_id", "transaction_id", "amount_vnd", "receiving_account_id", "environment"],
            transactions,
            ["tenant_id", "id", "amount_vnd", "receiving_account_id", "environment"],
        ),
        _fk(
            n("settlements_intent_fk"),
            ["tenant_id", "intent_id", "intent_amount_vnd", "receiving_account_id", "environment"],
            intents,
            ["tenant_id", "id", "amount_vnd", "receiving_account_id", "environment"],
        ),
        _fk(
            n("settlements_review_case_fk"),
            ["review_case_id", "tenant_id", "transaction_id"],
            review_cases,
            ["id", "tenant_id", "transaction_id"],
        ),
        sa.CheckConstraint("amount_vnd = intent_amount_vnd", name=n("settlements_exact_amount_ck")),
        sa.CheckConstraint(
            "origin = 'auto' OR (review_case_id IS NOT NULL AND resolved_by IS NOT NULL)",
            name=n("settlements_operator_provenance_ck"),
        ),
        sa.CheckConstraint(_ENVIRONMENT, name=n("settlements_environment_ck")),
        sa.CheckConstraint(
            "origin IN ('auto', 'operator_review')", name=n("settlements_origin_ck")
        ),
    )

    op.create_table(
        n("outbox_events"),
        _uuid("event_id", primary_key=True),
        _tenant(),
        _str("event_type", 64),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        _str("aggregate_type", 64),
        _uuid("aggregate_id"),
        sa.Column("payload", _json(), nullable=False),
        sa.Column("trusted_scope", _json(), nullable=False),
        _str("status", 32),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        _str("lease_owner", 255, nullable=True),
        _ts("lease_until", nullable=True),
        sa.Column("lease_generation", sa.BigInteger(), nullable=False, server_default="0"),
        _ts("occurred_at"),
        _ts("available_at"),
        _ts("next_attempt_at", nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        _ts("published_at", nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'published', 'failed')", name=n("outbox_status_ck")
        ),
    )
    op.create_index(
        n("outbox_status_next_attempt_ix"), n("outbox_events"), ["status", "next_attempt_at"]
    )


def downgrade(op: Any, prefix: str = "pm_") -> None:
    """Drop the schema-v1 tables in reverse dependency order."""
    for name in (
        "outbox_events",
        "settlements",
        "review_cases",
        "provider_observations",
        "provider_transactions",
        "reconciliation_runs",
        "webhook_inbox",
        "payment_intents",
        "connection_reference_readiness",
        "connection_account_bindings",
        "provider_connections",
        "receiving_accounts",
        "merchants",
        "reference_profile_prefixes",
        "reference_profiles",
    ):
        op.drop_table(f"{prefix}{name}")
