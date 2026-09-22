"""SePay webhook verification and event keys, cases written from the SePay signature docs.

``X-SePay-Signature: sha256=<hex>`` over ``f"{timestamp}." + raw_body``, 10-digit
``X-SePay-Timestamp``, every secret of the rotation window tried. Not yet checked against a
real SePay Test delivery.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from payment_module.adapters.sepay.provider import SePayProvider, sign
from payment_module.domain.enums import EventKeyKind
from payment_module.domain.errors import WebhookAuthError
from payment_module.ports.provider import VerifiedDelivery

pytestmark = pytest.mark.contract

NOW = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)
TS = int(NOW.timestamp())
OLD, NEW = "whsec-old", "whsec-new"
TOLERANCE = 300
BODY = json.dumps({"id": 92704, "transferAmount": 150000}).encode()


def headers(body: bytes = BODY, *, secret: str = NEW, ts: int | str = TS) -> dict[str, str]:
    return {"X-SePay-Signature": sign(body, secret, ts), "X-SePay-Timestamp": str(ts)}


def verify(body: bytes, hdrs: dict[str, str], secrets: list[str] | None = None):
    return SePayProvider().verify(body, hdrs, secrets or [OLD, NEW], NOW, TOLERANCE)


def auth_code(body: bytes, hdrs: dict[str, str], secrets: list[str] | None = None) -> str:
    with pytest.raises(WebhookAuthError) as caught:
        verify(body, hdrs, secrets)
    return caught.value.code


def test_valid_signature_verifies_raw_bytes() -> None:
    verified = verify(BODY, headers())
    assert verified.raw_body == BODY
    assert verified.verified_at == NOW


def test_header_names_are_case_insensitive() -> None:
    hdrs = {key.lower(): value for key, value in headers().items()}
    assert verify(BODY, hdrs).raw_body == BODY


@pytest.mark.parametrize("secret", [OLD, NEW])
def test_every_secret_of_the_rotation_window_is_accepted(secret: str) -> None:
    assert verify(BODY, headers(secret=secret)).raw_body == BODY


def test_retired_secret_is_rejected() -> None:
    assert auth_code(BODY, headers(secret=OLD), [NEW]) == WebhookAuthError.INVALID_SIGNATURE


def test_upper_case_hex_digest_is_accepted() -> None:
    hdrs = headers()
    hdrs["X-SePay-Signature"] = "sha256=" + hdrs["X-SePay-Signature"][7:].upper()
    assert verify(BODY, hdrs).raw_body == BODY


@pytest.mark.parametrize("offset", [-TOLERANCE, TOLERANCE])
def test_timestamp_at_the_tolerance_edge_is_accepted(offset: int) -> None:
    assert verify(BODY, headers(ts=TS + offset)).raw_body == BODY


@pytest.mark.parametrize("offset", [-TOLERANCE - 1, TOLERANCE + 1])
def test_timestamp_past_the_tolerance_is_stale(offset: int) -> None:
    assert auth_code(BODY, headers(ts=TS + offset)) == WebhookAuthError.STALE_TIMESTAMP


@pytest.mark.parametrize("missing", ["X-SePay-Signature", "X-SePay-Timestamp"])
def test_missing_header(missing: str) -> None:
    hdrs = headers()
    del hdrs[missing]
    assert auth_code(BODY, hdrs) == WebhookAuthError.MISSING_HEADER


def test_signature_without_scheme_prefix_is_invalid() -> None:
    hdrs = headers()
    hdrs["X-SePay-Signature"] = hdrs["X-SePay-Signature"].removeprefix("sha256=")
    assert auth_code(BODY, hdrs) == WebhookAuthError.INVALID_SIGNATURE


@pytest.mark.parametrize("digest", ["zz" * 32, "ab" * 31, "ab" * 33, ""])
def test_malformed_hex_is_invalid(digest: str) -> None:
    hdrs = headers()
    hdrs["X-SePay-Signature"] = f"sha256={digest}"
    assert auth_code(BODY, hdrs) == WebhookAuthError.INVALID_SIGNATURE


@pytest.mark.parametrize("ts", ["123456789", "12345678901", "17585O0000", "-175850000"])
def test_timestamp_that_is_not_ten_digits_is_invalid(ts: str) -> None:
    hdrs = headers()
    hdrs["X-SePay-Timestamp"] = ts
    assert auth_code(BODY, hdrs) == WebhookAuthError.INVALID_SIGNATURE


def test_body_changed_after_signing_is_invalid() -> None:
    assert auth_code(BODY + b" ", headers()) == WebhookAuthError.INVALID_SIGNATURE


def test_signature_binds_the_timestamp() -> None:
    hdrs = headers()
    hdrs["X-SePay-Timestamp"] = str(TS + 1)
    assert auth_code(BODY, hdrs) == WebhookAuthError.INVALID_SIGNATURE


def test_large_body_is_verified_as_is() -> None:
    # The size limit is enforced by webhook ingestion before verification.
    body = json.dumps({"id": 1, "content": "x" * 70_000}).encode()
    assert verify(body, headers(body)).raw_body == body


def delivery(raw: bytes) -> VerifiedDelivery:
    return VerifiedDelivery(raw_body=raw, headers={}, verified_at=NOW)


@pytest.mark.parametrize(
    ("raw_id", "expected"),
    [
        (92704, "webhook:92704"),
        (0, "webhook:0"),
        ("92704", "webhook:92704"),
        ("9" * 64, "webhook:" + "9" * 64),
    ],
)
def test_event_key_from_numeric_id(raw_id: object, expected: str) -> None:
    key = SePayProvider().extract_event_key(delivery(json.dumps({"id": raw_id}).encode()))
    assert key is not None
    assert key.kind == EventKeyKind.PROVIDER_ID
    assert key.value == expected


@pytest.mark.parametrize(
    "raw",
    [
        json.dumps({"id": True}).encode(),
        json.dumps({"id": -1}).encode(),
        json.dumps({"id": "9" * 65}).encode(),
        json.dumps({"id": "12a"}).encode(),
        json.dumps({"id": "١٢٣"}).encode(),
        json.dumps({"id": 1.5}).encode(),
        json.dumps({"amount": 1}).encode(),
        json.dumps([1, 2]).encode(),
        b"not json",
        b"\xff\xfe{}",
    ],
)
def test_no_event_key_for_a_bad_or_missing_id(raw: bytes) -> None:
    assert SePayProvider().extract_event_key(delivery(raw)) is None
