"""Views built from storage rows carry plain strings; they must behave like enum members."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from payment_module.domain.enums import (
    AuthMode,
    ConnectionStatus,
    Environment,
    IntentStatus,
    MatchState,
    MerchantStatus,
    ProfileKind,
    ReceivingAccountStatus,
    ReconcileMode,
    ReconciliationRunStatus,
    ReviewCaseStatus,
    SettlementOrigin,
)
from payment_module.domain.events import PaymentSettled
from payment_module.domain.intent import transition_intent
from payment_module.domain.reference import NamedPrefix, ReferenceProfile
from payment_module.domain.transaction import transition_match_state
from payment_module.ports.resolvers import ProviderConnection


def test_status_enums_use_database_strings() -> None:
    assert [s.value for s in ReviewCaseStatus] == ["open", "resolved"]
    assert [s.value for s in MerchantStatus] == ["active", "disabled"]
    assert [s.value for s in ReceivingAccountStatus] == ["active", "disabled", "retired"]
    assert [s.value for s in ReconciliationRunStatus] == ["running", "completed", "failed"]
    assert [s.value for s in AuthMode] == ["hmac"]


def test_transitions_accept_plain_strings() -> None:
    assert transition_intent("expired", "paid") is IntentStatus.PAID  # type: ignore[arg-type]
    assert transition_match_state("recorded", "settled") is MatchState.SETTLED  # type: ignore[arg-type]


def test_reference_profile_coerces_kind_and_status() -> None:
    profile = ReferenceProfile(
        1, (NamedPrefix("topup", "TOP"),), 8, "0123456789", "generated", "active"
    )  # type: ignore[arg-type]
    assert profile.kind is ProfileKind.GENERATED
    legacy = ReferenceProfile(2, (), None, None, "legacy_import", "accepted_legacy")  # type: ignore[arg-type]
    assert legacy.kind is ProfileKind.LEGACY_IMPORT


def test_event_coerces_enum_fields() -> None:
    event = PaymentSettled(
        event_id=UUID(int=1),
        tenant_id="tenant-a",
        environment="live",  # type: ignore[arg-type]
        merchant_id=UUID(int=2),
        intent_id=UUID(int=3),
        transaction_id=UUID(int=4),
        settlement_id=UUID(int=5),
        receiving_account_id=UUID(int=6),
        amount_vnd=1,
        origin="auto",  # type: ignore[arg-type]
        settled_at=datetime(2026, 9, 22, tzinfo=UTC),
        host_ref_type="order",
        host_ref_id="1",
    )
    assert event.environment is Environment.LIVE
    assert event.origin is SettlementOrigin.AUTO


def test_port_dto_coerces_and_rejects_unknown() -> None:
    fields = {
        "id": UUID(int=1),
        "tenant_id": "tenant-a",
        "merchant_id": UUID(int=2),
        "environment": "test",
        "provider": "sepay",
        "locator": "loc",
        "status": "active",
        "reconcile_mode": "detect_only",
        "timestamp_tolerance_seconds": 300,
        "secret_ref": "ref",
        "api_credential_ref": None,
    }
    connection = ProviderConnection(**fields)  # type: ignore[arg-type]
    assert connection.status is ConnectionStatus.ACTIVE
    assert connection.reconcile_mode is ReconcileMode.DETECT_ONLY
    with pytest.raises(ValueError):
        ProviderConnection(**{**fields, "reconcile_mode": "auto"})  # type: ignore[arg-type]
