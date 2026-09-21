"""Outbox delivery port for hosts that fulfil asynchronously."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from payment_module.domain.events import JsonValue


@dataclass(frozen=True, slots=True)
class OutboxEventView:
    event_id: UUID
    event_type: str
    schema_version: int
    trusted_scope: Mapping[str, JsonValue]
    payload: Mapping[str, JsonValue]
    occurred_at: datetime


class OutboxPublisher(Protocol):
    async def publish(self, event: OutboxEventView) -> None:
        """Deliver at least once; consumers deduplicate on ``event_id``."""
        ...
