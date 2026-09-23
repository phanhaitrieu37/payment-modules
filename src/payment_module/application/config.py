"""Package settings shared by the use cases; every field has a safe default."""

from __future__ import annotations

import math
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

    :meth:`validate` also bounds every timing and limit field (see ``_INT_BOUNDS``), so the
    link horizon is always positive and at most a few months long.
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
        for name, (low, high) in _INT_BOUNDS.items():
            _check_int(name, getattr(self, name), low, high)
        rate = self.reconcile_rate_per_second
        if (
            isinstance(rate, bool)
            or not isinstance(rate, (int, float))
            or not math.isfinite(rate)
            or not 0 < rate <= _MAX_RATE_PER_SECOND
        ):
            raise InvalidConfiguration(
                "reconcile_rate_per_second must be a finite number in "
                f"(0, {_MAX_RATE_PER_SECOND}]; got {rate!r}"
            )
        if self.pii_retention_days is None:
            return
        if not isinstance(self.pii_retention_days, int) or isinstance(
            self.pii_retention_days, bool
        ):
            raise InvalidConfiguration(
                f"pii_retention_days must be an int or None; got {self.pii_retention_days!r}"
            )
        if self.pii_retention_days < 0:
            raise InvalidConfiguration("pii_retention_days must not be negative")
        if self.pii_retention_days > _MAX_RETENTION_DAYS:
            raise InvalidConfiguration(
                f"pii_retention_days must be at most {_MAX_RETENTION_DAYS}; "
                f"got {self.pii_retention_days}"
            )
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


# Inclusive (low, high) bounds. The upper bounds only catch typos and unit mix-ups: a
# read window of at most 31 days, a grace of at most 7 days, a lease of at most 1 hour.
_INT_BOUNDS: dict[str, tuple[int, int]] = {
    "lease_seconds": (1, 3600),
    "inbox_max_attempts": (1, 1000),
    "outbox_max_attempts": (1, 1000),
    "max_body_bytes": (1, 16 * 1024 * 1024),
    "reconcile_grace_seconds": (0, 7 * 24 * 3600),
    "reconcile_page_size": (1, 1000),
    "reconcile_window_hours": (1, 24 * 31),
    "late_settlement_days": (0, 3660),
}
_MAX_RATE_PER_SECOND = 1000.0
_MAX_RETENTION_DAYS = 36600


def _check_int(name: str, value: object, low: int, high: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidConfiguration(f"{name} must be an int; got {value!r}")
    if not low <= value <= high:
        raise InvalidConfiguration(f"{name} must be in [{low}, {high}]; got {value}")


def retry_delay(attempts: int) -> timedelta:
    """Backoff after ``attempts`` tries: ``min(2**attempts, 300)`` seconds."""
    return timedelta(seconds=min(2 ** max(attempts, 0), 300))
