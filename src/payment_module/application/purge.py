"""Retention: drop raw payloads and free text once ``purge_after`` has passed.

``purge_after`` is written at intake from ``pii_retention_days``; ``None`` there means rows
are never purged. Only personal data goes: the raw body and headers of finished inbox rows,
and the memo (and normalized copy) of observations and facts. Free text is kept while its
fact can still be decided again (not final, or with an open review case), because rematch
and review read reference tokens from it, and while a sighting from the other source can
still be linked to the fact (see :meth:`PaymentModuleConfig.link_horizon`), because linking
compares those tokens too. Identity, dedup and amount columns stay, so dedup
and the audit trail keep working after a purge.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from payment_module.application.config import PaymentModuleConfig
from payment_module.ports.clock import Clock
from payment_module.ports.metrics import MetricsSink, increment_safely
from payment_module.ports.unit_of_work import UnitOfWorkFactory

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PurgeResult:
    inbox: int
    observations: int
    transactions: int

    @property
    def total(self) -> int:
        return self.inbox + self.observations + self.transactions


class PurgeExpiredPayloads:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        metrics: MetricsSink,
        config: PaymentModuleConfig,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._metrics = metrics
        self._config = config

    async def execute(self, limit: int = 500) -> PurgeResult:
        """Purge at most ``limit`` rows per table in one transaction; call again while
        ``total`` is positive to drain a backlog."""
        now = self._clock.now()
        linkable_after = now - self._config.link_horizon()
        async with self._uow_factory() as uow:
            result = PurgeResult(
                inbox=await uow.inbox.purge_expired(now, limit),
                observations=await uow.observations.purge_expired(
                    now, limit, linkable_after=linkable_after
                ),
                transactions=await uow.transactions.purge_expired(
                    now, limit, linkable_after=linkable_after
                ),
            )
            await uow.commit()
        for table, count in (
            ("inbox", result.inbox),
            ("observations", result.observations),
            ("transactions", result.transactions),
        ):
            for _ in range(count):
                increment_safely(self._metrics, "payloads_purged_total", {"table": table})
        if result.total:
            logger.info(
                "payment_payloads_purged",
                extra={
                    "inbox": result.inbox,
                    "observations": result.observations,
                    "transactions": result.transactions,
                },
            )
        return result

    async def drain(self, limit: int = 500, max_batches: int = 20) -> int:
        """Run :meth:`execute` until nothing is left or ``max_batches`` ran; rows purged."""
        total = 0
        for _ in range(max_batches):
            purged = (await self.execute(limit)).total
            total += purged
            if purged == 0:
                break
        return total
