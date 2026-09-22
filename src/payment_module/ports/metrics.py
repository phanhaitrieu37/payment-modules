"""Counter port for operational alerts; hosts plug in their metrics backend."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Protocol

_NO_TAGS: Mapping[str, str] = MappingProxyType({})


class MetricsSink(Protocol):
    def increment(self, name: str, tags: Mapping[str, str] = _NO_TAGS) -> None:
        """Add one to counter ``name``. Tags carry internal ids only, never personal data."""
        ...


class NoOpMetricsSink:
    def increment(self, name: str, tags: Mapping[str, str] = _NO_TAGS) -> None:
        return None
