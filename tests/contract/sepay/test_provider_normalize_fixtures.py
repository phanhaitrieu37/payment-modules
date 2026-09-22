"""Normalization of SePay webhook bodies and API v2 rows.

The fixtures under ``tests/fixtures/sepay/doc_derived`` are built from the SePay docs, not
captured from SePay Test (``_provenance``); replace them with real anonymized payloads once
the SePay Test scenarios have run.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from payment_module.adapters.sepay.payload import normalize_api_row
from payment_module.adapters.sepay.provider import SePayProvider
from payment_module.domain.enums import Direction, IdentityKind, ObservationSource
from payment_module.domain.errors import DomainError
from payment_module.ports.provider import VerifiedDelivery

pytestmark = pytest.mark.contract

FIXTURES = Path(__file__).parents[2] / "fixtures" / "sepay" / "doc_derived"
NOW = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)


def load(name: str) -> dict[str, Any]:
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert data["_provenance"].startswith("doc_derived")
    return data


def normalize(data: dict[str, Any], **provider_options: Any):
    raw = json.dumps(data).encode()
    delivery = VerifiedDelivery(raw_body=raw, headers={}, verified_at=NOW)
    return SePayProvider(**provider_options).normalize(delivery)


def test_incoming_webhook() -> None:
    obs = normalize(load("webhook_in.json"))
    assert obs.source == ObservationSource.WEBHOOK
    assert obs.identity_kind == IdentityKind.WEBHOOK_ID
    assert obs.source_tx_id == "92704"
    assert obs.reported_account_key == "VIETCOMBANK|1017588888|"
    assert obs.direction == Direction.IN
    assert obs.amount.value == 150_000
    assert obs.code == "SUBK7M2QX"
    assert obs.content == "SUBK7M2QX chuyen tien"
    assert obs.bank_reference == "FT24012345678"
    assert obs.provider_time is None


def test_outgoing_webhook() -> None:
    obs = normalize(load("webhook_out.json"))
    assert obs.direction == Direction.OUT
    assert obs.amount.value == 20_000
    assert obs.code is None


def test_webhook_and_api_rows_of_one_transfer_share_account_key_and_bank_reference() -> None:
    webhook = normalize(load("webhook_in.json"))
    api_in, api_out = (normalize_api_row(row) for row in load("api_v2_transactions.json")["data"])
    assert api_in.reported_account_key == webhook.reported_account_key
    assert api_in.bank_reference == webhook.bank_reference
    assert api_in.amount == webhook.amount
    assert api_in.direction == webhook.direction
    # The two id spaces are never compared: numeric webhook id, UUID API id.
    assert api_in.source_tx_id != webhook.source_tx_id
    assert api_in.source == ObservationSource.API
    assert api_in.identity_kind == IdentityKind.API_ID
    assert api_in.provider_time is None
    assert api_out.direction == Direction.OUT
    assert api_out.amount.value == 20_000


def test_memo_is_cut_to_the_configured_length() -> None:
    data = load("webhook_in.json") | {"content": "x" * 600}
    assert len(normalize(data).content) == 512
    assert len(normalize(data, memo_max_chars=40).content) == 40


def test_bank_reference_is_trimmed_upper_cased_and_blank_is_none() -> None:
    assert normalize(load("webhook_in.json") | {"referenceCode": " ft1 "}).bank_reference == "FT1"
    assert normalize(load("webhook_in.json") | {"referenceCode": "  "}).bank_reference is None
    assert normalize(load("webhook_in.json") | {"referenceCode": None}).bank_reference is None


def test_overlong_code_is_dropped_but_memo_kept() -> None:
    obs = normalize(load("webhook_in.json") | {"code": "C" * 65})
    assert obs.code is None
    assert obs.content == "SUBK7M2QX chuyen tien"


def test_sub_account_is_part_of_the_account_key() -> None:
    obs = normalize(load("webhook_in.json") | {"subAccount": "va 01"})
    assert obs.reported_account_key == "VIETCOMBANK|1017588888|VA01"


@pytest.mark.parametrize(
    ("amount", "expected"),
    [(150000, 150_000), (150000.0, 150_000), ("150000", 150_000), (0, 0)],
)
def test_webhook_amounts(amount: object, expected: int) -> None:
    assert normalize(load("webhook_in.json") | {"transferAmount": amount}).amount.value == expected


@pytest.mark.parametrize(
    "change",
    [
        {"transferAmount": 1500.5},
        {"transferAmount": True},
        {"transferAmount": -1},
        {"transferAmount": "150000.00"},
        {"transferAmount": None},
        {"transferAmount": 2**63},
        {"transferAmount": "9223372036854775808"},
        {"id": True},
        {"id": -3},
        {"id": "9" * 65},
        {"id": int("9" * 300)},
        {"id": None},
        {"gateway": ""},
        {"accountNumber": None},
        {"accountNumber": "10|17"},
        {"content": 12},
    ],
)
def test_bad_webhook_payload_raises_a_value_error(change: dict[str, Any]) -> None:
    with pytest.raises((ValueError, DomainError)):
        normalize(load("webhook_in.json") | change)


@pytest.mark.parametrize("raw", [b"\xff\xfe", b"[]", b"{", b"null"])
def test_body_that_is_not_a_utf8_json_object_raises_a_value_error(raw: bytes) -> None:
    delivery = VerifiedDelivery(raw_body=raw, headers={}, verified_at=NOW)
    with pytest.raises(ValueError):
        SePayProvider().normalize(delivery)


@pytest.mark.parametrize(
    ("amount_in", "expected"),
    [("150000.00", 150_000), ("150000", 150_000), (150000, 150_000), (150000.0, 150_000)],
)
def test_api_amounts(amount_in: object, expected: int) -> None:
    row = load("api_v2_transactions.json")["data"][0] | {"amount_in": amount_in}
    assert normalize_api_row(row).amount.value == expected


@pytest.mark.parametrize(
    "change",
    [
        {"amount_in": "150000.50"},
        {"amount_in": "1,500"},
        {"amount_in": True},
        {"amount_in": None},
        {"amount_in": "9223372036854775808"},
        {"amount_in": "9223372036854775808.00"},
        {"amount_out": "9223372036854775808"},
        {"id": ""},
        {"id": None},
        {"bank_brand_name": None},
        {"account_number": ""},
    ],
)
def test_bad_api_row_raises_a_value_error(change: dict[str, Any]) -> None:
    row = load("api_v2_transactions.json")["data"][0] | change
    with pytest.raises((ValueError, DomainError)):
        normalize_api_row(row)
