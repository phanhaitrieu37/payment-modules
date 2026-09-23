"""Domain events, schema version 1.

``(event_type, schema_version)`` is the contract key. Adding a field does not bump the
version; removing a field or changing its meaning does. Consumers ignore unknown fields and
deduplicate on ``event_id``.

``trusted_scope`` comes from committed rows, never from provider payloads. Payloads never
carry memo text, account numbers or holder names.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from enum import Enum, StrEnum
from typing import ClassVar
from uuid import UUID

from payment_module.domain.enums import (
    Direction,
    Environment,
    ReviewReason,
    ReviewResolution,
    SettlementOrigin,
    coerce_enum_fields,
)
from payment_module.domain.intent import ensure_aware

SCHEMA_VERSION = 1

type JsonValue = str | int | bool | list[JsonValue] | dict[str, JsonValue] | None


def _json_value(value: object) -> JsonValue:
    if isinstance(value, Enum):
        return str(value.value)
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    raise TypeError(f"unsupported event field type {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class _DomainEvent:
    event_type: ClassVar[str]
    schema_version: ClassVar[int] = SCHEMA_VERSION
    _enum_fields: ClassVar[dict[str, type[StrEnum]]] = {}

    event_id: UUID
    tenant_id: str
    environment: Environment

    def __post_init__(self) -> None:
        coerce_enum_fields(self, environment=Environment, **self._enum_fields)

    @property
    def trusted_scope(self) -> dict[str, JsonValue]:
        return {
            "tenant_id": self.tenant_id,
            "merchant_id": _json_value(getattr(self, "merchant_id", None)),
            "environment": self.environment.value,
        }

    def to_payload(self) -> dict[str, JsonValue]:
        """Canonical JSON-ready payload: UUIDs as strings, datetimes as ISO-8601 UTC."""
        payload: dict[str, JsonValue] = {
            item.name: _json_value(getattr(self, item.name)) for item in fields(self)
        }
        payload["event_type"] = self.event_type
        payload["schema_version"] = self.schema_version
        payload["trusted_scope"] = self.trusted_scope
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_payload(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class PaymentSettled(_DomainEvent):
    event_type: ClassVar[str] = "PaymentSettled"
    _enum_fields: ClassVar[dict[str, type[StrEnum]]] = {"origin": SettlementOrigin}

    merchant_id: UUID
    intent_id: UUID
    transaction_id: UUID
    settlement_id: UUID
    receiving_account_id: UUID
    amount_vnd: int
    origin: SettlementOrigin
    settled_at: datetime
    host_ref_type: str
    host_ref_id: str

    def __post_init__(self) -> None:
        _DomainEvent.__post_init__(self)
        ensure_aware(self.settled_at, "settled_at")


@dataclass(frozen=True, slots=True)
class PaymentNeedsReview(_DomainEvent):
    event_type: ClassVar[str] = "PaymentNeedsReview"
    _enum_fields: ClassVar[dict[str, type[StrEnum]]] = {
        "reason": ReviewReason,
        "direction": Direction,
    }

    merchant_id: UUID | None
    transaction_id: UUID
    review_case_id: UUID
    reason: ReviewReason
    candidate_intent_id: UUID | None
    amount_vnd: int
    direction: Direction


@dataclass(frozen=True, slots=True)
class ReviewResolved(_DomainEvent):
    event_type: ClassVar[str] = "ReviewResolved"
    _enum_fields: ClassVar[dict[str, type[StrEnum]]] = {"resolution": ReviewResolution}

    review_case_id: UUID
    transaction_id: UUID
    resolution: ReviewResolution
    resolved_by: str
    settlement_id: UUID | None
