"""Whole-VND amounts. Money is never compared or computed as a float."""

from __future__ import annotations

import re
from dataclasses import dataclass

from payment_module.domain.errors import InvalidAmount

_ASCII_DIGITS = re.compile(r"[0-9]+")

MAX_AMOUNT_VND = 2**63 - 1
"""The largest amount storage holds: amounts are PostgreSQL signed 64-bit ``BIGINT``."""


@dataclass(frozen=True, slots=True, order=True)
class AmountVnd:
    """A non-negative whole amount of Vietnamese dong, at most :data:`MAX_AMOUNT_VND`."""

    value: int

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or not isinstance(self.value, int):
            raise InvalidAmount(f"amount must be an int, got {type(self.value).__name__}")
        if self.value < 0:
            raise InvalidAmount("amount must not be negative")
        if self.value > MAX_AMOUNT_VND:
            raise InvalidAmount("amount exceeds the largest storable amount")

    @classmethod
    def parse(cls, raw: object) -> AmountVnd:
        """Read an amount from provider or host input.

        Accepts an ``int``, a ``float`` with no fractional part, or a string of ASCII
        digits. Rejects ``bool`` (a subclass of ``int``) and everything else.
        """
        if isinstance(raw, bool):
            raise InvalidAmount("a boolean is not an amount")
        if isinstance(raw, int):
            return cls(raw)
        if isinstance(raw, float):
            if not raw.is_integer():
                raise InvalidAmount("amount has a fractional part or is not finite")
            return cls(int(raw))
        if isinstance(raw, str):
            if not _ASCII_DIGITS.fullmatch(raw):
                raise InvalidAmount("amount string must contain only ASCII digits")
            return cls(int(raw))
        raise InvalidAmount(f"unsupported amount type {type(raw).__name__}")

    def __int__(self) -> int:
        return self.value
