"""Counter port for operational alerts; hosts plug in their metrics backend."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from types import MappingProxyType
from typing import Protocol

_NO_TAGS: Mapping[str, str] = MappingProxyType({})

logger = logging.getLogger(__name__)


class MetricsSink(Protocol):
    def increment(self, name: str, tags: Mapping[str, str] = _NO_TAGS) -> None:
        """Add one to counter ``name``. Tags carry internal ids only, never personal data.

        Must not raise; the package still calls it through :func:`increment_safely`, so a
        failing backend never changes a payment outcome or an HTTP answer.
        """
        ...


class NoOpMetricsSink:
    def increment(self, name: str, tags: Mapping[str, str] = _NO_TAGS) -> None:
        return None


def increment_safely(sink: MetricsSink, name: str, tags: Mapping[str, str] = _NO_TAGS) -> None:
    """Best-effort ``sink.increment``: a sink error is logged and swallowed."""
    try:
        sink.increment(name, tags)
    except Exception as exc:
        logger.warning(
            "payment_metrics_failed", extra={"metric": name, "error": type(exc).__name__}
        )
