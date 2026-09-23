"""Direction is whitelisted and never defaults to ``in``."""

from __future__ import annotations

import pytest

from payment_module.adapters.sepay.payload import api_direction, webhook_direction
from payment_module.domain.enums import Direction
from payment_module.domain.money import AmountVnd

pytestmark = pytest.mark.contract

_MISSING = object()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("in", Direction.IN),
        ("IN", Direction.IN),
        (" In ", Direction.IN),
        ("out", Direction.OUT),
        ("OUT", Direction.OUT),
        ("", Direction.UNKNOWN),
        (_MISSING, Direction.UNKNOWN),
        (None, Direction.UNKNOWN),
        ("transfer", Direction.UNKNOWN),
        ("credit", Direction.UNKNOWN),
        (1, Direction.UNKNOWN),
        (True, Direction.UNKNOWN),
    ],
)
def test_webhook_transfer_type(raw: object, expected: Direction) -> None:
    payload = {} if raw is _MISSING else {"transferType": raw}
    assert webhook_direction(payload.get("transferType")) == expected


@pytest.mark.parametrize(
    ("amount_in", "amount_out", "expected"),
    [
        (150_000, 0, Direction.IN),
        (0, 20_000, Direction.OUT),
        (0, 0, Direction.UNKNOWN),
        (150_000, 20_000, Direction.UNKNOWN),
    ],
)
def test_api_amounts(amount_in: int, amount_out: int, expected: Direction) -> None:
    assert api_direction(AmountVnd(amount_in), AmountVnd(amount_out)) == expected
