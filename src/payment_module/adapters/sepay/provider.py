"""SePay webhook provider: HMAC verification, event key, normalization, VietQR instruction.

Signature scheme (SePay docs, re-read 22/09/2026): ``X-SePay-Signature: sha256=<hex>`` where
``<hex>`` is HMAC-SHA256 of ``f"{timestamp}.".encode() + raw_body`` under the webhook secret,
and ``X-SePay-Timestamp`` is a 10-digit Unix timestamp. The raw bytes are verified as
received; the body is never re-serialized.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping, Sequence
from datetime import datetime

from payment_module.adapters.sepay.payload import (
    DEFAULT_MEMO_MAX_CHARS,
    load_object,
    normalize_webhook,
    webhook_id,
)
from payment_module.adapters.sepay.vietqr import build_vietqr
from payment_module.domain.errors import WebhookAuthError
from payment_module.domain.intent import IntentView
from payment_module.ports.provider import (
    EventKey,
    NormalizedObservation,
    ReceivingAccountView,
    TransferInstruction,
    VerifiedDelivery,
)

SIGNATURE_HEADER = "x-sepay-signature"
TIMESTAMP_HEADER = "x-sepay-timestamp"
SIGNATURE_SCHEME = "sha256="

_TIMESTAMP = re.compile(r"[0-9]{10}")
_HEX_DIGEST = re.compile(r"[0-9a-fA-F]{64}")


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def sign(raw_body: bytes, secret: str, timestamp: int | str) -> str:
    """The ``X-SePay-Signature`` value SePay sends for this body and timestamp."""
    message = f"{timestamp}.".encode() + raw_body
    return SIGNATURE_SCHEME + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


class SePayProvider:
    code = "sepay"

    def __init__(self, *, memo_max_chars: int = DEFAULT_MEMO_MAX_CHARS) -> None:
        self._memo_max_chars = memo_max_chars

    def verify(
        self,
        raw_body: bytes,
        headers: Mapping[str, str],
        secrets: Sequence[str],
        now: datetime,
        tolerance_s: int,
    ) -> VerifiedDelivery:
        given = _header(headers, SIGNATURE_HEADER)
        raw_timestamp = _header(headers, TIMESTAMP_HEADER)
        if given is None or raw_timestamp is None:
            raise WebhookAuthError(WebhookAuthError.MISSING_HEADER)
        given, raw_timestamp = given.strip(), raw_timestamp.strip()
        digest = given[len(SIGNATURE_SCHEME) :]
        if (
            not given.startswith(SIGNATURE_SCHEME)
            or not _HEX_DIGEST.fullmatch(digest)
            or not raw_timestamp.isascii()
            or not _TIMESTAMP.fullmatch(raw_timestamp)
        ):
            raise WebhookAuthError(WebhookAuthError.INVALID_SIGNATURE)
        expected = [
            sign(raw_body, secret, raw_timestamp)[len(SIGNATURE_SCHEME) :] for secret in secrets
        ]
        # Every secret is compared, so the time taken does not reveal which one matched.
        matches = [hmac.compare_digest(candidate, digest.lower()) for candidate in expected]
        if not any(matches):
            raise WebhookAuthError(WebhookAuthError.INVALID_SIGNATURE)
        if abs(now.timestamp() - int(raw_timestamp)) > tolerance_s:
            raise WebhookAuthError(WebhookAuthError.STALE_TIMESTAMP)
        return VerifiedDelivery(raw_body=raw_body, headers=dict(headers), verified_at=now)

    def extract_event_key(self, verified: VerifiedDelivery) -> EventKey | None:
        try:
            data = load_object(verified.raw_body)
        except ValueError:
            return None
        provider_id = webhook_id(data)
        return None if provider_id is None else EventKey.provider_id(provider_id)

    def normalize(self, verified: VerifiedDelivery) -> NormalizedObservation:
        return normalize_webhook(load_object(verified.raw_body), self._memo_max_chars)

    def build_instruction(
        self, intent: IntentView, account: ReceivingAccountView
    ) -> TransferInstruction:
        """``qr_payload`` is ``None`` when the account has no bank BIN; the BIN is never
        guessed from the bank code."""
        qr_payload = None
        if account.bank_bin is not None:
            qr_payload = build_vietqr(
                bank_bin=account.bank_bin,
                account_number=account.account_number,
                amount_vnd=intent.amount.value,
                payment_reference=intent.payment_reference,
            )
        return TransferInstruction(
            bank_code=account.bank_code,
            account_number=account.account_number,
            account_name=account.account_name,
            amount=intent.amount,
            payment_reference=intent.payment_reference,
            qr_payload=qr_payload,
        )
