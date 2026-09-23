from __future__ import annotations

import pytest

from payment_module.domain.errors import InvalidPaymentReference
from payment_module.domain.reference import PaymentReference, tokens_from


@pytest.mark.parametrize(
    ("code", "content", "expected"),
    [
        (None, None, []),
        ("", "", []),
        ("SUB12345678", None, ["SUB12345678"]),
        (None, "thanh toan SUB12345678 cam on", ["THANH", "TOAN", "SUB12345678", "CAM", "ON"]),
        (None, "sub12345678", ["SUB12345678"]),
        (None, ".SUB12345678.", ["SUB12345678"]),
        (None, "(SUB12345678)", ["SUB12345678"]),
        (None, "SUB12345678/TOP87654321", ["SUB12345678", "TOP87654321"]),
        # '-' and '_' belong to a token: surrounding them does not strip them.
        (None, "-SUB12345678-", ["-SUB12345678-"]),
        (None, "_SUB12345678_", ["_SUB12345678_"]),
        (None, "SUB-123_45", ["SUB-123_45"]),
        # Glued to other text: stays one longer token, never split to force a match.
        (None, "XSUB12345678", ["XSUB12345678"]),
        (None, "SUB12345678ABC", ["SUB12345678ABC"]),
        # Non-ASCII characters separate tokens and are dropped.
        (None, "Nguyễn Văn A SUB12345678", ["NGUY", "N", "V", "N", "A", "SUB12345678"]),
        (None, "SUB12345678​X", ["SUB12345678", "X"]),
        # Code first, then content, duplicates removed.
        ("SUB12345678", "ck SUB12345678", ["SUB12345678", "CK"]),
        ("SUB12345678", "TOP87654321", ["SUB12345678", "TOP87654321"]),
        ("  ", "\t\n", []),
    ],
)
def test_tokens_from(code: str | None, content: str | None, expected: list[str]) -> None:
    assert tokens_from(code, content) == list(dict.fromkeys(expected))


def test_payment_reference_normalize_strips_and_upper_cases() -> None:
    assert PaymentReference.normalize("  sub12345678 \n").value == "SUB12345678"


@pytest.mark.parametrize("raw", ["", "   ", "SUB 123", "SUB.123", "sub123", "SUBĐ1"])
def test_payment_reference_must_be_one_normalized_token(raw: str) -> None:
    with pytest.raises(InvalidPaymentReference):
        PaymentReference(raw)


def test_normalized_reference_round_trips_through_tokens() -> None:
    reference = PaymentReference.normalize("top-2026_ab")
    assert tokens_from(None, f"memo {reference.value.lower()} end") == [
        "MEMO",
        str(reference),
        "END",
    ]
