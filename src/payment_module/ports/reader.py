"""Optional reconciliation capability: page through provider transactions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from payment_module.ports.provider import NormalizedObservation
from payment_module.ports.resolvers import ProviderConnection


@dataclass(frozen=True, slots=True)
class Window:
    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class Page:
    observations: tuple[NormalizedObservation, ...]
    next_cursor: str | None


class TransactionReader(Protocol):
    """Implemented only by providers that offer a transaction API."""

    async def list_page(
        self,
        connection: ProviderConnection,
        credential: str,
        cursor: str | None,
        window: Window,
    ) -> Page: ...
