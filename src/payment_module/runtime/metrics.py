"""A :class:`~payment_module.ports.metrics.MetricsSink` that writes one structured log line
per increment, for hosts without a metrics backend."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from types import MappingProxyType

_NO_TAGS: Mapping[str, str] = MappingProxyType({})


class LoggingMetricsSink:
    def __init__(self, logger: logging.Logger | None = None, level: int = logging.INFO) -> None:
        self._logger = logger or logging.getLogger("payment_module.metrics")
        self._level = level

    def increment(self, name: str, tags: Mapping[str, str] = _NO_TAGS) -> None:
        self._logger.log(self._level, "payment_metric", extra={"metric": name, "tags": dict(tags)})
