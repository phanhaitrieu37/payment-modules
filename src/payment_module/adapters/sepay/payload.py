"""Coercion of SePay webhook bodies and API v2 transaction rows into observations.

Field names come from the SePay documentation read on 22/09/2026 and have **not** been
checked against a real SePay Test payload yet:

* webhook: ``id``, ``gateway``, ``accountNumber``, ``subAccount``, ``transferType``,
  ``transferAmount``, ``code``, ``content``, ``referenceCode``;
* API v2 (``GET /v2/transactions``): ``id`` (UUID), ``bank_brand_name``,
  ``account_number``, ``va`` (older docs: ``sub_account``), ``amount_in``, ``amount_out``,
  ``code``, ``transaction_content``, ``reference_number``, ``bank_account_id``.

When real payloads disagree, fix the names here; the schema does not change. Every helper
raises ``ValueError`` (or a ``DomainError`` that is one) so a malformed body is quarantined.
The provider's ``transactionDate`` is ignored until its format and time zone are verified.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from payment_module.domain.account_identity import account_key
from payment_module.domain.enums import Direction, IdentityKind, ObservationSource
from payment_module.domain.errors import InvalidAmount
from payment_module.domain.money import AmountVnd
from payment_module.ports.provider import NormalizedObservation

DEFAULT_MEMO_MAX_CHARS = 512
MAX_WEBHOOK_ID_DIGITS = 64
MAX_API_ID_CHARS = 255
MAX_CODE_CHARS = 64
MAX_BANK_REFERENCE_CHARS = 255

_DIGITS = re.compile(r"[0-9]+")
_WHOLE_DECIMAL = re.compile(r"([0-9]+)(?:\.0+)?")
_WEBHOOK_DIRECTIONS = {"in": Direction.IN, "out": Direction.OUT}


def load_object(raw_body: bytes) -> dict[str, Any]:
    """The JSON object of a UTF-8 body; anything else raises ``ValueError``."""
    data = json.loads(raw_body.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("payload is not a JSON object")
    return data


def webhook_id(data: Mapping[str, Any]) -> str | None:
    """The webhook ``id`` as a digit string: an ``int >= 0`` or ASCII digits, at most 64
    digits either way so the inbox event key always fits its column."""
    raw = data.get("id")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return str(raw) if 0 <= raw < 10**MAX_WEBHOOK_ID_DIGITS else None
    if (
        isinstance(raw, str)
        and len(raw) <= MAX_WEBHOOK_ID_DIGITS
        and raw.isascii()
        and _DIGITS.fullmatch(raw)
    ):
        return raw
    return None


def webhook_direction(raw: object) -> Direction:
    """``transferType`` ``in``/``out`` in any case; anything else, or absent, is ``unknown``.

    Never defaults to ``in``: an unrecognized direction must not settle an intent.
    """
    if isinstance(raw, str):
        return _WEBHOOK_DIRECTIONS.get(raw.strip().lower(), Direction.UNKNOWN)
    return Direction.UNKNOWN


def api_amount(raw: object) -> AmountVnd:
    """An API amount: the webhook amount rules, plus decimal strings with a zero fraction.

    Older SePay API docs print amounts as ``"2277000.00"``; a non-zero fraction is rejected.
    """
    if isinstance(raw, str):
        matched = _WHOLE_DECIMAL.fullmatch(raw)
        if matched is None:
            raise InvalidAmount("amount string must be whole dong")
        return AmountVnd(int(matched.group(1)))
    return AmountVnd.parse(raw)


def api_direction(amount_in: AmountVnd, amount_out: AmountVnd) -> Direction:
    """``in`` only when money came in and none went out, ``out`` the reverse, else ``unknown``."""
    if amount_in.value > 0 and amount_out.value == 0:
        return Direction.IN
    if amount_out.value > 0 and amount_in.value == 0:
        return Direction.OUT
    return Direction.UNKNOWN


def optional_text(raw: object, field: str) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"{field} must be a string")
    return raw


def memo(raw: object, field: str, max_chars: int) -> str | None:
    text = optional_text(raw, field)
    return None if text is None else text[:max_chars]


def code(raw: object) -> str | None:
    """The provider-extracted code; one longer than any valid reference is dropped, the memo
    still carries the text."""
    text = optional_text(raw, "code")
    if text is None:
        return None
    text = text.strip()
    return text if 0 < len(text) <= MAX_CODE_CHARS else None


def bank_reference(raw: object) -> str | None:
    """The bank's own transfer reference, trimmed and upper-cased; blank becomes ``None``."""
    text = optional_text(raw, "bank reference")
    if text is None:
        return None
    text = text.strip().upper()
    if len(text) > MAX_BANK_REFERENCE_CHARS:
        raise ValueError("bank reference is too long")
    return text or None


def required_text(data: Mapping[str, Any], field: str) -> str:
    raw = data.get(field)
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{field} is missing")
    return raw


def normalize_webhook(data: Mapping[str, Any], memo_max_chars: int) -> NormalizedObservation:
    source_tx_id = webhook_id(data)
    if source_tx_id is None:
        raise ValueError("webhook id is missing or invalid")
    return NormalizedObservation(
        source=ObservationSource.WEBHOOK,
        source_tx_id=source_tx_id,
        identity_kind=IdentityKind.WEBHOOK_ID,
        reported_account_key=account_key(
            required_text(data, "gateway"),
            required_text(data, "accountNumber"),
            optional_text(data.get("subAccount"), "subAccount"),
        ),
        direction=webhook_direction(data.get("transferType")),
        amount=AmountVnd.parse(data.get("transferAmount")),
        code=code(data.get("code")),
        content=memo(data.get("content"), "content", memo_max_chars),
        bank_reference=bank_reference(data.get("referenceCode")),
        provider_time=None,
    )


def normalize_api_row(
    row: Mapping[str, Any], memo_max_chars: int = DEFAULT_MEMO_MAX_CHARS
) -> NormalizedObservation:
    """One API v2 transaction row; the API id (a UUID) is kept as an opaque string."""
    source_tx_id = required_text(row, "id").strip()
    if len(source_tx_id) > MAX_API_ID_CHARS:
        raise ValueError("api transaction id is too long")
    amount_in = api_amount(row.get("amount_in", 0))
    amount_out = api_amount(row.get("amount_out", 0))
    direction = api_direction(amount_in, amount_out)
    sub_account = row.get("va", row.get("sub_account"))
    return NormalizedObservation(
        source=ObservationSource.API,
        source_tx_id=source_tx_id,
        identity_kind=IdentityKind.API_ID,
        reported_account_key=account_key(
            required_text(row, "bank_brand_name"),
            required_text(row, "account_number"),
            optional_text(sub_account, "va"),
        ),
        direction=direction,
        amount=amount_out if direction == Direction.OUT else amount_in,
        code=code(row.get("code")),
        content=memo(row.get("transaction_content"), "transaction_content", memo_max_chars),
        bank_reference=bank_reference(row.get("reference_number")),
        provider_time=None,
    )
