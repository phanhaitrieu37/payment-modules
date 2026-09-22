"""Package settings shared by the use cases; every field has a safe default."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True, slots=True)
class PaymentModuleConfig:
    """``pii_retention_days=None`` keeps raw bodies until the host purges them.

    ``header_allowlist`` names the only request headers stored with an inbox row; signatures
    are never stored.

    Reconciliation: an API observation that no webhook fact claims within
    ``reconcile_grace_seconds`` becomes a fact of its own; each read covers the last
    ``reconcile_window_hours``, ``reconcile_page_size`` rows per page.
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

    def owner(self) -> str:
        """Lease owner written on claims: ``worker_owner`` or ``hostname:pid``."""
        return self.worker_owner or f"{socket.gethostname()}:{os.getpid()}"

    def purge_after(self, received_at: datetime) -> datetime | None:
        if self.pii_retention_days is None:
            return None
        return received_at + timedelta(days=self.pii_retention_days)


def retry_delay(attempts: int) -> timedelta:
    """Backoff after ``attempts`` tries: ``min(2**attempts, 300)`` seconds."""
    return timedelta(seconds=min(2 ** max(attempts, 0), 300))
