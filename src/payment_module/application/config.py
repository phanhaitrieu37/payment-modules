"""Package settings shared by the use cases; every field has a safe default."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta


class InvalidConfiguration(ValueError):
    """A :class:`PaymentModuleConfig` the module refuses to be built with."""


@dataclass(frozen=True, slots=True)
class PaymentModuleConfig:
    """``pii_retention_days=None`` keeps raw bodies until the host purges them.

    ``header_allowlist`` names the only request headers stored with an inbox row; signatures
    are never stored.

    Reconciliation: an API observation that no webhook fact claims within
    ``reconcile_grace_seconds`` becomes a fact of its own; each read covers the last
    ``reconcile_window_hours``, ``reconcile_page_size`` rows per page.

    ``late_settlement_days`` is how long after expiry late money is still supported for an
    intent: its reference profile cannot be retired before then.

    A fact's free text is what links a sighting from the other source to it, so it is kept
    for :meth:`link_horizon` at least; :meth:`validate` rejects a shorter
    ``pii_retention_days``.
    """

    lease_seconds: int = 60
    inbox_max_attempts: int = 10
    outbox_max_attempts: int = 10
    max_body_bytes: int = 65536
    header_allowlist: tuple[str, ...] = ("x-sepay-timestamp", "content-type")
    pii_retention_days: int | None = None
    worker_owner: str | None = None
    reconcile_grace_seconds: int = 900
    reconcile_page_size: int = 100
    reconcile_window_hours: int = 24
    reconcile_rate_per_second: float = 2.0
    late_settlement_days: int = 30

    def owner(self) -> str:
        """Lease owner written on claims: ``worker_owner`` or ``hostname:pid``."""
        return self.worker_owner or f"{socket.gethostname()}:{os.getpid()}"

    def link_horizon(self) -> timedelta:
        """How long a fact may still be linked to a sighting from the other source.

        One read window, the grace before an unclaimed API sighting becomes a fact of its
        own, and one more window as a margin for slow processing and late reads.
        """
        window = timedelta(hours=self.reconcile_window_hours)
        return window + timedelta(seconds=self.reconcile_grace_seconds) + window

    def validate(self) -> None:
        """Raise :class:`InvalidConfiguration` for settings that would lose money evidence."""
        if self.pii_retention_days is None:
            return
        if self.pii_retention_days < 0:
            raise InvalidConfiguration("pii_retention_days must not be negative")
        if timedelta(days=self.pii_retention_days) < self.link_horizon():
            raise InvalidConfiguration(
                "pii_retention_days must cover the cross-source link horizon "
                "(2 * reconcile_window_hours + reconcile_grace_seconds = "
                f"{self.link_horizon()}); got {self.pii_retention_days} day(s)"
            )

    def purge_after(self, received_at: datetime) -> datetime | None:
        if self.pii_retention_days is None:
            return None
        return received_at + timedelta(days=self.pii_retention_days)


def retry_delay(attempts: int) -> timedelta:
    """Backoff after ``attempts`` tries: ``min(2**attempts, 300)`` seconds."""
    return timedelta(seconds=min(2 ** max(attempts, 0), 300))
