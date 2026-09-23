from __future__ import annotations

import pytest

from payment_module.domain.account_identity import account_key
from payment_module.domain.errors import InvalidAccountIdentity


@pytest.mark.parametrize(
    ("bank", "account", "sub", "expected"),
    [
        ("VCB", "0123456789", None, "VCB|0123456789|"),
        ("vcb", " 0123 456 789 ", "", "VCB|0123456789|"),
        ("MBBank", "0123456789", " va01 ", "MBBANK|0123456789|VA01"),
        ("ACB", "12\t34", "  ", "ACB|1234|"),
    ],
)
def test_account_key_is_canonical(bank: str, account: str, sub: str | None, expected: str) -> None:
    assert account_key(bank, account, sub) == expected


def test_payload_key_and_fingerprint_compare_equal() -> None:
    reported = account_key("vcb", "0123 456 789", None)
    fingerprint = account_key("VCB", "0123456789", "")
    assert reported == fingerprint


@pytest.mark.parametrize(
    ("bank", "account", "sub"),
    [("", "0123", None), ("VCB", "  ", None), ("VCB", "01|23", None), ("VCB", "0123", "A|B")],
)
def test_account_key_rejects_missing_or_ambiguous_parts(
    bank: str, account: str, sub: str | None
) -> None:
    with pytest.raises(InvalidAccountIdentity):
        account_key(bank, account, sub)
