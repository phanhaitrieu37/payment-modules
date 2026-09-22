from __future__ import annotations

import math
from decimal import Decimal

import pytest

from payment_module.domain.errors import InvalidAmount
from payment_module.domain.money import MAX_AMOUNT_VND, AmountVnd


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0, 0),
        (150_000, 150_000),
        (150_000.0, 150_000),
        ("150000", 150_000),
        ("007", 7),
        (2**63 - 1, 2**63 - 1),
        (str(2**63 - 1), 2**63 - 1),
    ],
)
def test_parse_accepts_whole_amounts(raw: object, expected: int) -> None:
    assert AmountVnd.parse(raw) == AmountVnd(expected)


@pytest.mark.parametrize(
    "raw",
    [
        True,
        False,
        -1,
        150_000.5,
        math.nan,
        math.inf,
        -1.0,
        "",
        "-5",
        "1.0",
        "1e3",
        " 100",
        "\u0661\u0660\u0660",  # Arabic-Indic digits are not ASCII digits
        Decimal("100"),
        None,
        [100],
    ],
)
def test_parse_rejects_non_whole_amounts(raw: object) -> None:
    with pytest.raises(InvalidAmount):
        AmountVnd.parse(raw)


@pytest.mark.parametrize("raw", [True, 1.0, "1", -1])
def test_constructor_only_takes_non_negative_int(raw: object) -> None:
    with pytest.raises(InvalidAmount):
        AmountVnd(raw)  # type: ignore[arg-type]


def test_amounts_compare_by_value() -> None:
    assert AmountVnd(1) < AmountVnd(2)
    assert AmountVnd(5) == AmountVnd.parse("5")
    assert int(AmountVnd(9)) == 9


@pytest.mark.parametrize("raw", [2**63, str(2**63), "9" * 300, float(2**64)])
def test_parse_rejects_amounts_beyond_signed_bigint(raw: object) -> None:
    assert MAX_AMOUNT_VND == 2**63 - 1
    with pytest.raises(InvalidAmount):
        AmountVnd.parse(raw)
