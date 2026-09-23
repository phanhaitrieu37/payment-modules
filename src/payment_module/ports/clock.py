"""Server time source, replaceable in tests."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Current timezone-aware time."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)
