from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest

from payment_module.domain.enums import (
    Direction,
    Environment,
    ReviewReason,
    ReviewResolution,
    SettlementOrigin,
)
from payment_module.domain.events import PaymentNeedsReview, PaymentSettled, ReviewResolved


def uid(n: int) -> UUID:
    return UUID(int=n)


SETTLED = PaymentSettled(
    event_id=uid(1),
    tenant_id="tenant-a",
    environment=Environment.LIVE,
    merchant_id=uid(2),
    intent_id=uid(3),
    transaction_id=uid(4),
    settlement_id=uid(5),
    receiving_account_id=uid(6),
    amount_vnd=150_000,
    origin=SettlementOrigin.AUTO,
    settled_at=datetime(2026, 9, 22, 17, 0, tzinfo=timezone(timedelta(hours=7))),
    host_ref_type="order",
    host_ref_id="ORD-1",
)


def test_payment_settled_snapshot() -> None:
    assert SETTLED.to_json() == (
        '{"amount_vnd":150000,"environment":"live",'
        '"event_id":"00000000-0000-0000-0000-000000000001",'
        '"event_type":"PaymentSettled","host_ref_id":"ORD-1","host_ref_type":"order",'
        '"intent_id":"00000000-0000-0000-0000-000000000003",'
        '"merchant_id":"00000000-0000-0000-0000-000000000002","origin":"auto",'
        '"receiving_account_id":"00000000-0000-0000-0000-000000000006",'
        '"schema_version":1,"settled_at":"2026-09-22T10:00:00+00:00",'
        '"settlement_id":"00000000-0000-0000-0000-000000000005","tenant_id":"tenant-a",'
        '"transaction_id":"00000000-0000-0000-0000-000000000004",'
        '"trusted_scope":{"environment":"live",'
        '"merchant_id":"00000000-0000-0000-0000-000000000002","tenant_id":"tenant-a"}}'
    )


def test_payment_needs_review_snapshot() -> None:
    event = PaymentNeedsReview(
        event_id=uid(10),
        tenant_id="tenant-a",
        environment=Environment.TEST,
        merchant_id=None,
        transaction_id=uid(11),
        review_case_id=uid(12),
        reason=ReviewReason.TENANT_MISMATCH,
        candidate_intent_id=None,
        amount_vnd=50_000,
        direction=Direction.IN,
    )
    assert json.loads(event.to_json()) == {
        "amount_vnd": 50_000,
        "candidate_intent_id": None,
        "direction": "in",
        "environment": "test",
        "event_id": str(uid(10)),
        "event_type": "PaymentNeedsReview",
        "merchant_id": None,
        "reason": "TENANT_MISMATCH",
        "review_case_id": str(uid(12)),
        "schema_version": 1,
        "tenant_id": "tenant-a",
        "transaction_id": str(uid(11)),
        "trusted_scope": {"environment": "test", "merchant_id": None, "tenant_id": "tenant-a"},
    }


def test_review_resolved_payload_has_version_and_scope() -> None:
    event = ReviewResolved(
        event_id=uid(20),
        tenant_id="tenant-a",
        environment=Environment.LIVE,
        review_case_id=uid(21),
        transaction_id=uid(22),
        resolution=ReviewResolution.MARK_EXTERNAL,
        resolved_by="operator-1",
        settlement_id=None,
    )
    payload = event.to_payload()
    assert payload["schema_version"] == 1
    assert payload["event_type"] == "ReviewResolved"
    assert payload["resolution"] == "mark_external"
    assert payload["trusted_scope"] == {
        "environment": "live",
        "merchant_id": None,
        "tenant_id": "tenant-a",
    }


@pytest.mark.parametrize("forbidden", ["memo", "content", "account_number", "holder_name"])
def test_payloads_carry_no_personal_data(forbidden: str) -> None:
    assert forbidden not in SETTLED.to_payload()


def test_settled_at_must_be_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        replace(SETTLED, settled_at=datetime(2026, 9, 22, 10, 0))


def test_settled_time_is_serialized_in_utc() -> None:
    assert SETTLED.to_payload()["settled_at"] == datetime(2026, 9, 22, 10, tzinfo=UTC).isoformat()
