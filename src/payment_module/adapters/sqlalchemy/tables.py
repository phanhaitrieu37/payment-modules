"""SQLAlchemy Core tables of the payment module, attached to the host's ``MetaData``.

``define_tables(metadata, prefix="pm_")`` adds the 15 package tables to the metadata the host
already owns, so the host's ``create_all`` and Alembic autogenerate see them. Core is used on
purpose: the package never touches the host's ORM mapper registry.

Production schemas are created by the frozen migration ``migrations.schema_v1``, not from
this module; a parity test keeps both identical on PostgreSQL.

Every money invariant that can be written portably lives in the database: unique keys,
``CHECK`` constraints and composite foreign keys that carry ``tenant_id`` (and, where it
matters, ``merchant_id`` and ``environment``) so that no row can point across tenants,
merchants or Test/Live. Enum ``CHECK`` constraints are generated from
:mod:`payment_module.domain.enums`.

Composite foreign keys and the unique key each one targets
(PostgreSQL requires a unique constraint on exactly the referenced columns):

====================================  ===========================================================
Referenced unique key                 Referenced by
====================================  ===========================================================
merchants (tenant_id, id)             receiving_accounts, provider_connections, payment_intents
receiving_accounts                    connection_account_bindings, payment_intents,
(tenant_id, merchant_id,              provider_transactions
environment, id)
provider_connections                  connection_account_bindings
(tenant_id, merchant_id,
environment, id)
provider_connections                  connection_reference_readiness, provider_observations
(tenant_id, environment, id)
provider_connections (tenant_id, id)  webhook_inbox, reconciliation_runs
reference_profiles PK (version)       reference_profile_prefixes, connection_reference_readiness,
                                      payment_intents
reference_profile_prefixes PK         payment_intents (reference_profile_version,
(profile_version, name)               reference_prefix_name)
payment_intents                       payment_intents.superseded_by_intent_id (self)
(tenant_id, merchant_id,
environment, id)
payment_intents                       review_cases.candidate_intent_id
(tenant_id, environment, id)
payment_intents (tenant_id, id,       settlements (intent side, intent_amount_vnd)
amount_vnd, receiving_account_id,
environment)
webhook_inbox                         provider_observations.inbox_id
(tenant_id, connection_id, id)
reconciliation_runs                   provider_observations.reconciliation_run_id
(tenant_id, connection_id, id)
provider_transactions (tenant_id,     provider_observations.transaction_id (with
environment, provider,                reported_account_key)
provider_account_key, id)
provider_transactions                 provider_transactions.duplicate_of_transaction_id (self),
(tenant_id, environment, id)          review_cases.transaction_id
provider_transactions (tenant_id,     settlements (transaction side, amount_vnd)
id, amount_vnd,
receiving_account_id, environment)
review_cases                          settlements.review_case_id
(id, tenant_id, transaction_id)
====================================  ===========================================================

Nullable foreign-key columns use MATCH SIMPLE, so the check is skipped when any column is
NULL. The deliberate NULL cases are: observation ``transaction_id`` before linking, review
``candidate_intent_id``, intent ``superseded_by_intent_id``, fact
``duplicate_of_transaction_id`` and the fact's ``merchant_id``/``receiving_account_id`` pair,
which a ``CHECK`` forces to be NULL together so the receiver FK cannot be bypassed.
An intent with ``reference_prefix_name`` NULL is only valid for a ``legacy_import`` profile;
that cannot be written as a portable ``CHECK`` and is enforced by the application.

One bank account or virtual account has exactly one owner (tenant + merchant) per
environment within an installation: ``UQ(environment, account_fingerprint)`` is not scoped
by tenant.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from payment_module.domain.enums import (
    AuthMode,
    ConnectionStatus,
    Direction,
    Environment,
    EventKeyKind,
    FirstSource,
    IdentityKind,
    InboxStatus,
    IntentStatus,
    LinkMethod,
    LinkStatus,
    MatchState,
    MerchantStatus,
    ObservationSource,
    OutboxStatus,
    ProfileKind,
    ProfileStatus,
    ReadinessStatus,
    ReceivingAccountStatus,
    ReconcileMode,
    ReconciliationRunStatus,
    ReviewCaseStatus,
    ReviewReason,
    ReviewResolution,
    SettlementOrigin,
)

TABLE_COUNT = 15
DEFAULT_TIMESTAMP_TOLERANCE_SECONDS = 300


def json_type() -> sa.types.TypeEngine[object]:
    """Portable JSON that becomes ``JSONB`` on PostgreSQL."""
    return sa.JSON().with_variant(JSONB(), "postgresql")


def _in(column: str, enum_type: type[StrEnum]) -> str:
    values = ", ".join(f"'{member.value}'" for member in enum_type)
    return f"{column} IN ({values})"


def _ts(name: str, *, nullable: bool = False, server_now: bool = False) -> sa.Column[object]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.func.now() if server_now else None,
    )


def _tenant() -> sa.Column[str]:
    return sa.Column("tenant_id", sa.String(128), nullable=False)


def _environment() -> sa.Column[str]:
    return sa.Column("environment", sa.String(16), nullable=False)


def _uuid(name: str, *, nullable: bool = False, primary_key: bool = False) -> sa.Column[object]:
    return sa.Column(name, sa.Uuid(), nullable=nullable, primary_key=primary_key)


@dataclass(frozen=True, slots=True)
class PaymentTables:
    """The package tables, bound to one ``MetaData`` and one table-name prefix."""

    prefix: str
    merchants: sa.Table
    receiving_accounts: sa.Table
    provider_connections: sa.Table
    connection_account_bindings: sa.Table
    reference_profiles: sa.Table
    reference_profile_prefixes: sa.Table
    connection_reference_readiness: sa.Table
    payment_intents: sa.Table
    webhook_inbox: sa.Table
    reconciliation_runs: sa.Table
    provider_observations: sa.Table
    provider_transactions: sa.Table
    settlements: sa.Table
    review_cases: sa.Table
    outbox_events: sa.Table


def define_tables(metadata: sa.MetaData, prefix: str = "pm_") -> PaymentTables:
    """Add the payment tables to ``metadata`` and return them.

    ``prefix`` is prepended to every table, constraint and index name.
    """
    p = prefix

    def n(name: str) -> str:
        return f"{p}{name}"

    merchants = sa.Table(
        n("merchants"),
        metadata,
        _uuid("id", primary_key=True),
        _tenant(),
        sa.Column("host_merchant_ref", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        _ts("created_at", server_now=True),
        sa.UniqueConstraint("tenant_id", "id", name=n("merchants_tenant_id_uq")),
        sa.UniqueConstraint(
            "tenant_id", "host_merchant_ref", name=n("merchants_tenant_host_ref_uq")
        ),
        sa.CheckConstraint(_in("status", MerchantStatus), name=n("merchants_status_ck")),
    )

    receiving_accounts = sa.Table(
        n("receiving_accounts"),
        metadata,
        _uuid("id", primary_key=True),
        _tenant(),
        _uuid("merchant_id"),
        _environment(),
        sa.Column("bank_code", sa.String(32), nullable=False),
        sa.Column("bank_bin", sa.String(16), nullable=True),
        sa.Column("account_number", sa.String(64), nullable=False),
        sa.Column("sub_account", sa.String(64), nullable=False, server_default=""),
        sa.Column("account_number_masked", sa.String(64), nullable=False),
        sa.Column("holder_name", sa.String(255), nullable=False),
        sa.Column("account_fingerprint", sa.String(255), nullable=False),
        sa.Column("provider_account_ref", sa.String(255), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.UniqueConstraint(
            "environment", "account_fingerprint", name=n("accounts_env_fingerprint_uq")
        ),
        sa.UniqueConstraint(
            "tenant_id", "merchant_id", "environment", "id", name=n("accounts_scope_id_uq")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "merchant_id"],
            [f"{n('merchants')}.tenant_id", f"{n('merchants')}.id"],
            name=n("accounts_merchant_fk"),
        ),
        sa.CheckConstraint(_in("environment", Environment), name=n("accounts_environment_ck")),
        sa.CheckConstraint(_in("status", ReceivingAccountStatus), name=n("accounts_status_ck")),
    )

    provider_connections = sa.Table(
        n("provider_connections"),
        metadata,
        _uuid("id", primary_key=True),
        _tenant(),
        _uuid("merchant_id"),
        sa.Column("provider", sa.String(32), nullable=False),
        _environment(),
        sa.Column("locator", sa.String(128), nullable=False),
        sa.Column("secret_ref", sa.String(255), nullable=False),
        sa.Column("api_credential_ref", sa.String(255), nullable=True),
        sa.Column("auth_mode", sa.String(32), nullable=False),
        sa.Column(
            "reconcile_mode",
            sa.String(32),
            nullable=False,
            server_default=ReconcileMode.DETECT_ONLY.value,
        ),
        sa.Column("reconcile_evidence_ref", sa.String(255), nullable=True),
        sa.Column(
            "timestamp_tolerance_seconds",
            sa.Integer(),
            nullable=False,
            server_default=str(DEFAULT_TIMESTAMP_TOLERANCE_SECONDS),
        ),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default=ConnectionStatus.PENDING.value,
        ),
        sa.Column("status_changed_by", sa.String(255), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["tenant_id", "merchant_id"],
            [f"{n('merchants')}.tenant_id", f"{n('merchants')}.id"],
            name=n("connections_merchant_fk"),
        ),
        sa.CheckConstraint(_in("environment", Environment), name=n("connections_environment_ck")),
        sa.CheckConstraint(_in("auth_mode", AuthMode), name=n("connections_auth_mode_ck")),
        sa.CheckConstraint(
            _in("reconcile_mode", ReconcileMode), name=n("connections_reconcile_mode_ck")
        ),
        sa.CheckConstraint(
            f"reconcile_mode = '{ReconcileMode.DETECT_ONLY.value}' "
            "OR reconcile_evidence_ref IS NOT NULL",
            name=n("connections_auto_settle_evidence_ck"),
        ),
        sa.CheckConstraint(
            "timestamp_tolerance_seconds BETWEEN 60 AND 7200",
            name=n("connections_tolerance_ck"),
        ),
        sa.CheckConstraint(_in("status", ConnectionStatus), name=n("connections_status_ck")),
    )

    connection_account_bindings = sa.Table(
        n("connection_account_bindings"),
        metadata,
        _tenant(),
        _uuid("merchant_id"),
        _environment(),
        _uuid("connection_id", primary_key=True),
        _uuid("receiving_account_id", primary_key=True),
        sa.Column("created_by", sa.String(255), nullable=False),
        _ts("created_at", server_now=True),
        sa.ForeignKeyConstraint(
            ["tenant_id", "merchant_id", "environment", "connection_id"],
            [
                f"{n('provider_connections')}.{column}"
                for column in ("tenant_id", "merchant_id", "environment", "id")
            ],
            name=n("bindings_connection_fk"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "merchant_id", "environment", "receiving_account_id"],
            [
                f"{n('receiving_accounts')}.{column}"
                for column in ("tenant_id", "merchant_id", "environment", "id")
            ],
            name=n("bindings_account_fk"),
        ),
        sa.CheckConstraint(_in("environment", Environment), name=n("bindings_environment_ck")),
    )

    reference_profiles = sa.Table(
        n("reference_profiles"),
        metadata,
        sa.Column("version", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("suffix_length", sa.Integer(), nullable=True),
        sa.Column("alphabet", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        _ts("created_at", server_now=True),
        _ts("activated_at", nullable=True),
        sa.Column("activated_by", sa.String(255), nullable=True),
        _ts("retired_at", nullable=True),
        sa.Column("retired_by", sa.String(255), nullable=True),
        sa.CheckConstraint(_in("kind", ProfileKind), name=n("profiles_kind_ck")),
        sa.CheckConstraint(_in("status", ProfileStatus), name=n("profiles_status_ck")),
        sa.CheckConstraint(
            f"kind = '{ProfileKind.LEGACY_IMPORT.value}' "
            "OR (suffix_length BETWEEN 1 AND 30 AND alphabet IS NOT NULL)",
            name=n("profiles_generated_shape_ck"),
        ),
        sa.Index(
            n("profiles_one_active_uq"),
            "status",
            unique=True,
            postgresql_where=sa.text(f"status = '{ProfileStatus.ACTIVE.value}'"),
        ),
    )

    reference_profile_prefixes = sa.Table(
        n("reference_profile_prefixes"),
        metadata,
        sa.Column("profile_version", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("prefix", sa.String(16), nullable=False),
        _ts("created_at", server_now=True),
        sa.UniqueConstraint("profile_version", "prefix", name=n("prefixes_version_prefix_uq")),
        sa.ForeignKeyConstraint(
            ["profile_version"],
            [f"{n('reference_profiles')}.version"],
            name=n("prefixes_profile_fk"),
        ),
        sa.CheckConstraint("length(prefix) BETWEEN 2 AND 5", name=n("prefixes_prefix_len_ck")),
        sa.CheckConstraint("length(name) BETWEEN 1 AND 32", name=n("prefixes_name_len_ck")),
    )

    connection_reference_readiness = sa.Table(
        n("connection_reference_readiness"),
        metadata,
        _uuid("id", primary_key=True),
        _tenant(),
        _uuid("connection_id"),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        _environment(),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("checklist", json_type(), nullable=False),
        sa.Column("evidence_ref", sa.String(255), nullable=True),
        sa.Column("verified_by", sa.String(255), nullable=True),
        _ts("verified_at", nullable=True),
        sa.UniqueConstraint(
            "connection_id", "profile_version", "environment", name=n("readiness_key_uq")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "environment", "connection_id"],
            [
                f"{n('provider_connections')}.{column}"
                for column in ("tenant_id", "environment", "id")
            ],
            name=n("readiness_connection_fk"),
        ),
        sa.ForeignKeyConstraint(
            ["profile_version"],
            [f"{n('reference_profiles')}.version"],
            name=n("readiness_profile_fk"),
        ),
        sa.CheckConstraint(_in("environment", Environment), name=n("readiness_environment_ck")),
        sa.CheckConstraint(_in("status", ReadinessStatus), name=n("readiness_status_ck")),
    )

    payment_intents = sa.Table(
        n("payment_intents"),
        metadata,
        _uuid("id", primary_key=True),
        _tenant(),
        _uuid("merchant_id"),
        _environment(),
        _uuid("receiving_account_id"),
        sa.Column("amount_vnd", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="VND"),
        sa.Column("beneficiary_snapshot", json_type(), nullable=False),
        sa.Column("payment_reference", sa.String(64), nullable=False),
        sa.Column("reference_profile_version", sa.Integer(), nullable=False),
        sa.Column("reference_prefix_name", sa.String(64), nullable=True),
        sa.Column("host_ref_type", sa.String(64), nullable=False),
        sa.Column("host_ref_id", sa.String(255), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("request_fingerprint", sa.String(128), nullable=False),
        _ts("expires_at"),
        sa.Column("status", sa.String(32), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["tenant_id", "merchant_id"],
            [f"{n('merchants')}.tenant_id", f"{n('merchants')}.id"],
            name=n("intents_merchant_fk"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "merchant_id", "environment", "receiving_account_id"],
            [
                f"{n('receiving_accounts')}.{column}"
                for column in ("tenant_id", "merchant_id", "environment", "id")
            ],
            name=n("intents_account_fk"),
        ),
        sa.ForeignKeyConstraint(
            ["reference_profile_version"],
            [f"{n('reference_profiles')}.version"],
            name=n("intents_profile_fk"),
        ),
        sa.ForeignKeyConstraint(
            ["reference_profile_version", "reference_prefix_name"],
            [
                f"{n('reference_profile_prefixes')}.profile_version",
                f"{n('reference_profile_prefixes')}.name",
            ],
            name=n("intents_prefix_fk"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "merchant_id", "environment", "superseded_by_intent_id"],
            [
                f"{n('payment_intents')}.{column}"
                for column in ("tenant_id", "merchant_id", "environment", "id")
            ],
            name=n("intents_superseded_by_fk"),
        ),
        sa.CheckConstraint("amount_vnd > 0", name=n("intents_amount_positive_ck")),
        sa.CheckConstraint("currency = 'VND'", name=n("intents_currency_ck")),
        sa.CheckConstraint(_in("environment", Environment), name=n("intents_environment_ck")),
        sa.CheckConstraint(_in("status", IntentStatus), name=n("intents_status_ck")),
        sa.Index(n("intents_account_reference_ix"), "receiving_account_id", "payment_reference"),
    )

    webhook_inbox = sa.Table(
        n("webhook_inbox"),
        metadata,
        _uuid("id", primary_key=True),
        _tenant(),
        _uuid("connection_id"),
        sa.Column("event_key", sa.String(255), nullable=False),
        sa.Column("event_key_kind", sa.String(32), nullable=False),
        sa.Column("body_sha256", sa.String(64), nullable=False),
        sa.Column("raw_body", sa.LargeBinary(), nullable=True),
        sa.Column("headers", json_type(), nullable=True),
        _ts("received_at"),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        _ts("next_attempt_at", nullable=True),
        sa.Column("lease_owner", sa.String(255), nullable=True),
        _ts("lease_until", nullable=True),
        sa.Column("lease_generation", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        _ts("purge_after", nullable=True),
        _ts("raw_purged_at", nullable=True),
        sa.UniqueConstraint("connection_id", "event_key", name=n("inbox_event_key_uq")),
        sa.UniqueConstraint("tenant_id", "connection_id", "id", name=n("inbox_scope_id_uq")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            [f"{n('provider_connections')}.tenant_id", f"{n('provider_connections')}.id"],
            name=n("inbox_connection_fk"),
        ),
        sa.CheckConstraint(_in("event_key_kind", EventKeyKind), name=n("inbox_event_key_kind_ck")),
        sa.CheckConstraint(_in("status", InboxStatus), name=n("inbox_status_ck")),
        sa.Index(n("inbox_status_next_attempt_ix"), "status", "next_attempt_at"),
    )

    reconciliation_runs = sa.Table(
        n("reconciliation_runs"),
        metadata,
        _uuid("id", primary_key=True),
        _tenant(),
        _uuid("connection_id"),
        _ts("window_from"),
        _ts("window_to"),
        sa.Column("cursor", sa.String(255), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("counts", json_type(), nullable=False),
        _ts("started_at"),
        _ts("finished_at", nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.UniqueConstraint("tenant_id", "connection_id", "id", name=n("recon_runs_scope_id_uq")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            [f"{n('provider_connections')}.tenant_id", f"{n('provider_connections')}.id"],
            name=n("recon_runs_connection_fk"),
        ),
        sa.CheckConstraint(_in("status", ReconciliationRunStatus), name=n("recon_runs_status_ck")),
    )

    provider_transactions = sa.Table(
        n("provider_transactions"),
        metadata,
        _uuid("id", primary_key=True),
        _tenant(),
        _uuid("merchant_id", nullable=True),
        _environment(),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_account_key", sa.String(255), nullable=False),
        _uuid("receiving_account_id", nullable=True),
        sa.Column("identity_kind", sa.String(32), nullable=False),
        sa.Column("identity_value", sa.String(255), nullable=False),
        sa.Column("dedup_key", sa.String(600), nullable=False),
        sa.Column("webhook_tx_id", sa.String(255), nullable=True),
        sa.Column("api_tx_id", sa.String(255), nullable=True),
        sa.Column("bank_reference", sa.String(255), nullable=True),
        sa.Column("amount_vnd", sa.BigInteger(), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        _ts("occurred_at", nullable=True),
        sa.Column("memo", sa.Text(), nullable=True),
        sa.Column("first_source", sa.String(32), nullable=False),
        sa.Column("match_state", sa.String(32), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["tenant_id", "merchant_id", "environment", "receiving_account_id"],
            [
                f"{n('receiving_accounts')}.{column}"
                for column in ("tenant_id", "merchant_id", "environment", "id")
            ],
            name=n("transactions_account_fk"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "environment", "duplicate_of_transaction_id"],
            [
                f"{n('provider_transactions')}.{column}"
                for column in ("tenant_id", "environment", "id")
            ],
            name=n("transactions_duplicate_of_fk"),
        ),
        sa.CheckConstraint("amount_vnd >= 0", name=n("transactions_amount_ck")),
        sa.CheckConstraint(
            "(merchant_id IS NULL) = (receiving_account_id IS NULL)",
            name=n("transactions_receiver_pair_ck"),
        ),
        sa.CheckConstraint(
            f"match_state <> '{MatchState.SETTLED.value}' "
            f"OR (receiving_account_id IS NOT NULL AND direction = '{Direction.IN.value}')",
            name=n("transactions_settled_shape_ck"),
        ),
        sa.CheckConstraint(_in("environment", Environment), name=n("transactions_environment_ck")),
        sa.CheckConstraint(
            _in("identity_kind", IdentityKind), name=n("transactions_identity_kind_ck")
        ),
        sa.CheckConstraint(_in("direction", Direction), name=n("transactions_direction_ck")),
        sa.CheckConstraint(
            _in("first_source", FirstSource), name=n("transactions_first_source_ck")
        ),
        sa.CheckConstraint(_in("match_state", MatchState), name=n("transactions_match_state_ck")),
        sa.Index(
            n("transactions_webhook_tx_uq"),
            "tenant_id",
            "environment",
            "provider",
            "provider_account_key",
            "webhook_tx_id",
            unique=True,
            postgresql_where=sa.text("webhook_tx_id IS NOT NULL"),
        ),
        sa.Index(
            n("transactions_api_tx_uq"),
            "tenant_id",
            "environment",
            "provider",
            "provider_account_key",
            "api_tx_id",
            unique=True,
            postgresql_where=sa.text("api_tx_id IS NOT NULL"),
        ),
        sa.Index(n("transactions_bank_reference_ix"), "bank_reference"),
    )

    provider_observations = sa.Table(
        n("provider_observations"),
        metadata,
        _uuid("id", primary_key=True),
        _tenant(),
        _environment(),
        _uuid("connection_id"),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("source_tx_id", sa.String(255), nullable=False),
        _uuid("inbox_id", nullable=True),
        _uuid("reconciliation_run_id", nullable=True),
        sa.Column("reported_account_key", sa.String(255), nullable=False),
        sa.Column("bank_reference", sa.String(255), nullable=True),
        sa.Column("amount_vnd", sa.BigInteger(), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        _ts("occurred_at", nullable=True),
        sa.Column("code", sa.String(64), nullable=True),
        sa.Column("memo", sa.Text(), nullable=True),
        sa.Column("normalized", json_type(), nullable=True),
        _uuid("transaction_id", nullable=True),
        sa.Column("link_method", sa.String(32), nullable=True),
        sa.Column("link_status", sa.String(32), nullable=False),
        _ts("observed_at"),
        _ts("purge_after", nullable=True),
        sa.UniqueConstraint(
            "provider",
            "source",
            "source_tx_id",
            "connection_id",
            name=n("observations_source_uq"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "environment", "connection_id"],
            [
                f"{n('provider_connections')}.{column}"
                for column in ("tenant_id", "environment", "id")
            ],
            name=n("observations_connection_fk"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "connection_id", "inbox_id"],
            [f"{n('webhook_inbox')}.{column}" for column in ("tenant_id", "connection_id", "id")],
            name=n("observations_inbox_fk"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "connection_id", "reconciliation_run_id"],
            [
                f"{n('reconciliation_runs')}.{column}"
                for column in ("tenant_id", "connection_id", "id")
            ],
            name=n("observations_recon_run_fk"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "environment", "provider", "reported_account_key", "transaction_id"],
            [
                f"{n('provider_transactions')}.{column}"
                for column in (
                    "tenant_id",
                    "environment",
                    "provider",
                    "provider_account_key",
                    "id",
                )
            ],
            name=n("observations_transaction_fk"),
        ),
        sa.CheckConstraint(
            f"(source = '{ObservationSource.WEBHOOK.value}' AND inbox_id IS NOT NULL) "
            f"OR (source = '{ObservationSource.API.value}' "
            "AND reconciliation_run_id IS NOT NULL)",
            name=n("observations_provenance_ck"),
        ),
        sa.CheckConstraint(_in("environment", Environment), name=n("observations_environment_ck")),
        sa.CheckConstraint(_in("source", ObservationSource), name=n("observations_source_ck")),
        sa.CheckConstraint(_in("direction", Direction), name=n("observations_direction_ck")),
        sa.CheckConstraint(_in("link_method", LinkMethod), name=n("observations_link_method_ck")),
        sa.CheckConstraint(_in("link_status", LinkStatus), name=n("observations_link_status_ck")),
        sa.Index(n("observations_link_status_ix"), "link_status"),
    )

    review_cases = sa.Table(
        n("review_cases"),
        metadata,
        _uuid("id", primary_key=True),
        _tenant(),
        _environment(),
        _uuid("transaction_id"),
        _uuid("candidate_intent_id", nullable=True),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("details", json_type(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("resolution", sa.String(32), nullable=True),
        sa.Column("resolution_ref", sa.String(255), nullable=True),
        sa.Column("resolved_by", sa.String(255), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        _ts("opened_at"),
        _ts("resolved_at", nullable=True),
        sa.UniqueConstraint(
            "id", "tenant_id", "transaction_id", name=n("review_cases_settlement_key_uq")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "environment", "transaction_id"],
            [
                f"{n('provider_transactions')}.{column}"
                for column in ("tenant_id", "environment", "id")
            ],
            name=n("review_cases_transaction_fk"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "environment", "candidate_intent_id"],
            [f"{n('payment_intents')}.{column}" for column in ("tenant_id", "environment", "id")],
            name=n("review_cases_candidate_fk"),
        ),
        sa.CheckConstraint(_in("environment", Environment), name=n("review_cases_environment_ck")),
        sa.CheckConstraint(_in("reason", ReviewReason), name=n("review_cases_reason_ck")),
        sa.CheckConstraint(_in("status", ReviewCaseStatus), name=n("review_cases_status_ck")),
        sa.CheckConstraint(
            _in("resolution", ReviewResolution), name=n("review_cases_resolution_ck")
        ),
        sa.Index(
            n("review_cases_one_open_uq"),
            "transaction_id",
            unique=True,
            postgresql_where=sa.text(f"status = '{ReviewCaseStatus.OPEN.value}'"),
        ),
    )

    settlements = sa.Table(
        n("settlements"),
        metadata,
        _uuid("id", primary_key=True),
        _tenant(),
        _environment(),
        _uuid("transaction_id"),
        _uuid("intent_id"),
        _uuid("receiving_account_id"),
        sa.Column("amount_vnd", sa.BigInteger(), nullable=False),
        sa.Column("intent_amount_vnd", sa.BigInteger(), nullable=False),
        sa.Column("origin", sa.String(32), nullable=False),
        _uuid("review_case_id", nullable=True),
        sa.Column("resolved_by", sa.String(255), nullable=True),
        _ts("settled_at"),
        sa.UniqueConstraint("transaction_id", name=n("settlements_transaction_uq")),
        sa.UniqueConstraint("intent_id", name=n("settlements_intent_uq")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "transaction_id", "amount_vnd", "receiving_account_id", "environment"],
            [
                f"{n('provider_transactions')}.{column}"
                for column in (
                    "tenant_id",
                    "id",
                    "amount_vnd",
                    "receiving_account_id",
                    "environment",
                )
            ],
            name=n("settlements_transaction_fk"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "intent_id", "intent_amount_vnd", "receiving_account_id", "environment"],
            [
                f"{n('payment_intents')}.{column}"
                for column in (
                    "tenant_id",
                    "id",
                    "amount_vnd",
                    "receiving_account_id",
                    "environment",
                )
            ],
            name=n("settlements_intent_fk"),
        ),
        sa.ForeignKeyConstraint(
            ["review_case_id", "tenant_id", "transaction_id"],
            [f"{n('review_cases')}.{column}" for column in ("id", "tenant_id", "transaction_id")],
            name=n("settlements_review_case_fk"),
        ),
        sa.CheckConstraint("amount_vnd = intent_amount_vnd", name=n("settlements_exact_amount_ck")),
        sa.CheckConstraint(
            f"origin = '{SettlementOrigin.AUTO.value}' "
            "OR (review_case_id IS NOT NULL AND resolved_by IS NOT NULL)",
            name=n("settlements_operator_provenance_ck"),
        ),
        sa.CheckConstraint(_in("environment", Environment), name=n("settlements_environment_ck")),
        sa.CheckConstraint(_in("origin", SettlementOrigin), name=n("settlements_origin_ck")),
    )

    outbox_events = sa.Table(
        n("outbox_events"),
        metadata,
        _uuid("event_id", primary_key=True),
        _tenant(),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("aggregate_type", sa.String(64), nullable=False),
        _uuid("aggregate_id"),
        sa.Column("payload", json_type(), nullable=False),
        sa.Column("trusted_scope", json_type(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_owner", sa.String(255), nullable=True),
        _ts("lease_until", nullable=True),
        sa.Column("lease_generation", sa.BigInteger(), nullable=False, server_default="0"),
        _ts("occurred_at"),
        _ts("available_at"),
        _ts("next_attempt_at", nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        _ts("published_at", nullable=True),
        sa.CheckConstraint(_in("status", OutboxStatus), name=n("outbox_status_ck")),
        sa.Index(n("outbox_status_next_attempt_ix"), "status", "next_attempt_at"),
    )

    return PaymentTables(
        prefix=prefix,
        merchants=merchants,
        receiving_accounts=receiving_accounts,
        provider_connections=provider_connections,
        connection_account_bindings=connection_account_bindings,
        reference_profiles=reference_profiles,
        reference_profile_prefixes=reference_profile_prefixes,
        connection_reference_readiness=connection_reference_readiness,
        payment_intents=payment_intents,
        webhook_inbox=webhook_inbox,
        reconciliation_runs=reconciliation_runs,
        provider_observations=provider_observations,
        provider_transactions=provider_transactions,
        settlements=settlements,
        review_cases=review_cases,
        outbox_events=outbox_events,
    )
