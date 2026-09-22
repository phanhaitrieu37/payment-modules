"""Optional reconciliation capability: page through provider transactions."""

from __future__ import annotations

from dataclasses import dataclass, field
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
    """One page of observations; ``next_cursor`` is ``None`` when the window is exhausted.

    ``webhook_success_ids`` are the rows the provider reports as already delivered by
    webhook. It only feeds a configuration alert, never a matching decision.

    ``invalid_rows`` counts the rows that could not be normalized; ``invalid_ids`` names those
    whose provider id could be read, so a skipped row stays traceable after the window moves.
    """

    observations: tuple[NormalizedObservation, ...]
    next_cursor: str | None
    webhook_success_ids: frozenset[str] = field(default_factory=frozenset)
    invalid_rows: int = 0
    invalid_ids: tuple[str, ...] = ()


class TransactionReadError(Exception):
    """The provider did not return a page (throttled, server error, bad response).

    ``status`` is the HTTP status when there was one; ``retry_after`` the seconds the
    provider asked to wait. The checkpoint never moves past a failed read.
    """

    def __init__(
        self, reason: str, *, status: int | None = None, retry_after: float | None = None
    ) -> None:
        self.reason = reason
        self.status = status
        self.retry_after = retry_after
        super().__init__(f"transaction read failed: {reason}")


class TransactionReader(Protocol):
    """Implemented only by providers that offer a transaction API."""

    async def list_page(
        self,
        connection: ProviderConnection,
        credential: str,
        cursor: str | None,
        window: Window,
        account_ref: str | None = None,
    ) -> Page:
        """The page at ``cursor`` (the first page when ``None``) of ``window``.

        ``account_ref`` restricts the read to one provider-side account so a company-level
        credential never scans other merchants' accounts. Raises
        :class:`TransactionReadError` when no page could be read.
        """
        ...
