"""A provider for application tests, written against the provider port only.

Signature: hex HMAC-SHA256 of ``f"{timestamp}." + raw_body`` under any configured secret, in
``x-signature``; the Unix timestamp travels in ``x-sepay-timestamp`` so the default header
allowlist stores it. The body is a small JSON object::

    {"id": 101, "bank": "VCB", "account": "1000001", "sub": null, "amount": 150000,
     "direction": "in", "code": "SUB...", "content": "...", "bank_reference": "FT1"}
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from payment_module.domain.account_identity import account_key
from payment_module.domain.enums import Direction, IdentityKind, ObservationSource
from payment_module.domain.errors import WebhookAuthError
from payment_module.domain.intent import IntentView
from payment_module.domain.money import AmountVnd
from payment_module.ports.provider import (
    EventKey,
    NormalizedObservation,
    ReceivingAccountView,
    TransferInstruction,
    VerifiedDelivery,
)
from payment_module.ports.resolvers import ProviderConnection

TIMESTAMP_HEADER = "x-sepay-timestamp"
SIGNATURE_HEADER = "x-signature"


def signature(raw_body: bytes, secret: str, timestamp: int) -> str:
    message = f"{timestamp}.".encode() + raw_body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def signed_headers(raw_body: bytes, secret: str, timestamp: int) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Sepay-Timestamp": str(timestamp),
        "X-Signature": signature(raw_body, secret, timestamp),
    }


def body(
    tx_id: int | str | None,
    *,
    account: str = "1000001",
    bank: str = "VCB",
    sub: str | None = None,
    amount: int = 150_000,
    direction: str = "in",
    code: str | None = None,
    content: str | None = None,
    bank_reference: str | None = "FT0001",
) -> bytes:
    data: dict[str, Any] = {
        "bank": bank,
        "account": account,
        "sub": sub,
        "amount": amount,
        "direction": direction,
        "code": code,
        "content": content,
        "bank_reference": bank_reference,
    }
    if tx_id is not None:
        data["id"] = tx_id
    return json.dumps(data, sort_keys=True).encode()


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


class FakeProvider:
    code = "fake"

    def __init__(self) -> None:
        self.verify_calls = 0

    def verify(
        self,
        raw_body: bytes,
        headers: Mapping[str, str],
        secrets: Sequence[str],
        now: datetime,
        tolerance_s: int,
    ) -> VerifiedDelivery:
        self.verify_calls += 1
        raw_timestamp = _header(headers, TIMESTAMP_HEADER)
        given = _header(headers, SIGNATURE_HEADER)
        if raw_timestamp is None or given is None:
            raise WebhookAuthError(WebhookAuthError.MISSING_HEADER)
        if not raw_timestamp.isascii() or not raw_timestamp.isdigit():
            raise WebhookAuthError(WebhookAuthError.INVALID_SIGNATURE)
        timestamp = int(raw_timestamp)
        if not any(
            hmac.compare_digest(signature(raw_body, secret, timestamp), given) for secret in secrets
        ):
            raise WebhookAuthError(WebhookAuthError.INVALID_SIGNATURE)
        if abs(now.timestamp() - timestamp) > tolerance_s:
            raise WebhookAuthError(WebhookAuthError.STALE_TIMESTAMP)
        return VerifiedDelivery(raw_body=raw_body, headers=dict(headers), verified_at=now)

    def extract_event_key(self, verified: VerifiedDelivery) -> EventKey | None:
        try:
            data = json.loads(verified.raw_body)
        except ValueError:
            return None
        raw_id = data.get("id") if isinstance(data, dict) else None
        if isinstance(raw_id, bool):
            return None
        if isinstance(raw_id, int) and raw_id >= 0:
            return EventKey.provider_id(str(raw_id))
        if isinstance(raw_id, str) and raw_id.isascii() and raw_id.isdigit():
            return EventKey.provider_id(raw_id)
        return None

    def normalize(self, verified: VerifiedDelivery) -> NormalizedObservation:
        data = json.loads(verified.raw_body)
        try:
            return NormalizedObservation(
                source=ObservationSource.WEBHOOK,
                source_tx_id=str(data["id"]),
                identity_kind=IdentityKind.WEBHOOK_ID,
                reported_account_key=account_key(data["bank"], data["account"], data.get("sub")),
                direction=Direction(data["direction"]),
                amount=AmountVnd.parse(data["amount"]),
                code=data.get("code"),
                content=data.get("content"),
                bank_reference=data.get("bank_reference"),
                provider_time=None,
            )
        except (KeyError, TypeError) as exc:
            raise ValueError(f"payload field missing or of the wrong type: {exc}") from exc

    def build_instruction(
        self, intent: IntentView, account: ReceivingAccountView
    ) -> TransferInstruction:
        return TransferInstruction(
            bank_code=account.bank_code,
            account_number=account.account_number,
            account_name=account.account_name,
            amount=intent.amount,
            payment_reference=intent.payment_reference,
            qr_payload=None,
        )


class StaticSecretResolver:
    def __init__(self, secrets: Sequence[str], api_credential: str | None = None) -> None:
        self.secrets = list(secrets)
        self.credential = api_credential
        self.calls = 0

    async def webhook_secrets(self, connection: ProviderConnection) -> list[str]:
        self.calls += 1
        return list(self.secrets)

    async def api_credential(self, connection: ProviderConnection) -> str | None:
        return self.credential
