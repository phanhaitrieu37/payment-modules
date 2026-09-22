"""Provider port: verify deliveries, read their event key, normalize, build instructions."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from payment_module.domain.enums import (
    Direction,
    Environment,
    EventKeyKind,
    IdentityKind,
    ObservationSource,
    coerce_enum_fields,
)
from payment_module.domain.intent import IntentView
from payment_module.domain.money import AmountVnd


@dataclass(frozen=True, slots=True)
class VerifiedDelivery:
    """A raw webhook delivery whose signature and timestamp were verified."""

    raw_body: bytes
    headers: Mapping[str, str]
    verified_at: datetime


@dataclass(frozen=True, slots=True)
class EventKey:
    """Inbox dedup key: ``webhook:<id>`` from the provider, or ``sha256:<hex>`` of the body."""

    kind: EventKeyKind
    value: str

    def __post_init__(self) -> None:
        coerce_enum_fields(self, kind=EventKeyKind)

    @classmethod
    def provider_id(cls, provider_event_id: str) -> EventKey:
        return cls(EventKeyKind.PROVIDER_ID, f"webhook:{provider_event_id}")

    @classmethod
    def body_hash(cls, raw_body: bytes) -> EventKey:
        return cls(EventKeyKind.BODY_HASH, f"sha256:{hashlib.sha256(raw_body).hexdigest()}")


@dataclass(frozen=True, slots=True)
class NormalizedObservation:
    """One sighting of a transaction, as reported by the provider.

    ``reported_account_key`` is built with ``account_key`` from the payload's full account
    fields. ``code`` and ``content`` may hold personal data and must not be logged.
    """

    source: ObservationSource
    source_tx_id: str
    identity_kind: IdentityKind
    reported_account_key: str
    direction: Direction
    amount: AmountVnd
    code: str | None
    content: str | None
    bank_reference: str | None
    provider_time: datetime | None

    def __post_init__(self) -> None:
        coerce_enum_fields(
            self, source=ObservationSource, identity_kind=IdentityKind, direction=Direction
        )


@dataclass(frozen=True, slots=True)
class ReceivingAccountView:
    id: UUID
    tenant_id: str
    merchant_id: UUID
    environment: Environment
    bank_code: str
    account_number: str
    sub_account: str | None
    account_name: str

    def __post_init__(self) -> None:
        coerce_enum_fields(self, environment=Environment)


@dataclass(frozen=True, slots=True)
class TransferInstruction:
    """What the payer needs: beneficiary, exact amount and the reference to put in the memo."""

    bank_code: str
    account_number: str
    account_name: str
    amount: AmountVnd
    payment_reference: str
    qr_payload: str | None


class PaymentProvider(Protocol):
    @property
    def code(self) -> str: ...

    def verify(
        self,
        raw_body: bytes,
        headers: Mapping[str, str],
        secrets: Sequence[str],
        now: datetime,
        tolerance_s: int,
    ) -> VerifiedDelivery:
        """Verify signature and timestamp against every secret in the rotation window."""
        ...

    def extract_event_key(self, verified: VerifiedDelivery) -> EventKey | None:
        """Read only the provider id after verification; ``None`` sends the inbox row to
        quarantine under a body-hash key."""
        ...

    def normalize(self, verified: VerifiedDelivery) -> NormalizedObservation: ...

    def build_instruction(
        self, intent: IntentView, account: ReceivingAccountView
    ) -> TransferInstruction: ...
