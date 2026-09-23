"""Canonical account key shared by payload-reported keys and onboarding fingerprints.

The adapter derives ``provider_account_key`` from a verified payload and onboarding derives
``account_fingerprint`` from the full account number. Both use :func:`account_key`, so the
guard can compare them with plain string equality. Masked numbers must never be passed in.
"""

from __future__ import annotations

import re

from payment_module.domain.errors import InvalidAccountIdentity

_WHITESPACE = re.compile(r"\s+")
_SEPARATOR = "|"


def _canonical_part(value: str | None) -> str:
    if value is None:
        return ""
    part = _WHITESPACE.sub("", value).upper()
    if _SEPARATOR in part:
        raise InvalidAccountIdentity(f"account identity part must not contain {_SEPARATOR!r}")
    return part


def account_key(bank_code: str, account_number: str, sub_account: str | None) -> str:
    """Return ``BANK|ACCOUNT|SUB`` with whitespace removed and letters upper-cased.

    An absent or blank sub-account becomes the empty string.
    """
    bank = _canonical_part(bank_code)
    account = _canonical_part(account_number)
    if not bank or not account:
        raise InvalidAccountIdentity("bank code and account number are required")
    return _SEPARATOR.join((bank, account, _canonical_part(sub_account)))
