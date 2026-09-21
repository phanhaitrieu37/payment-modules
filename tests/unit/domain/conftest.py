"""Builders for domain snapshots used across the matching tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from payment_module.domain.enums import Direction, Environment, IntentStatus
from payment_module.domain.intent import IntentView
from payment_module.domain.matching.invariant_guard import ConnectionView
from payment_module.domain.money import AmountVnd
from payment_module.domain.transaction import TransactionView

TENANT = "tenant-a"
MERCHANT = UUID("00000000-0000-0000-0000-00000000000a")
ACCOUNT = UUID("00000000-0000-0000-0000-0000000000ac")
CONNECTION = UUID("00000000-0000-0000-0000-0000000000cc")
NOW = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)
REFERENCE = "SUBK7Q2M9"


@pytest.fixture
def connection() -> ConnectionView:
    return ConnectionView(CONNECTION, TENANT, MERCHANT, Environment.LIVE)


@pytest.fixture
def make_tx() -> Callable[..., TransactionView]:
    base = TransactionView(
        id=uuid4(),
        tenant_id=TENANT,
        environment=Environment.LIVE,
        provider_account_key="VCB|0123456789|",
        receiving_account_id=ACCOUNT,
        merchant_id=MERCHANT,
        amount=AmountVnd(150_000),
        direction=Direction.IN,
    )

    def build(**changes: object) -> TransactionView:
        return replace(base, **changes)

    return build


@pytest.fixture
def make_intent() -> Callable[..., IntentView]:
    base = IntentView(
        id=uuid4(),
        tenant_id=TENANT,
        merchant_id=MERCHANT,
        environment=Environment.LIVE,
        receiving_account_id=ACCOUNT,
        amount=AmountVnd(150_000),
        status=IntentStatus.AWAITING_PAYMENT,
        payment_reference=REFERENCE,
        expires_at=NOW + timedelta(minutes=15),
    )

    def build(**changes: object) -> IntentView:
        return replace(base, **changes)

    return build


@pytest.fixture
def account_id() -> UUID:
    return ACCOUNT


@pytest.fixture
def now() -> datetime:
    return NOW
