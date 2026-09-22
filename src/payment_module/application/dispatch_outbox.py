"""Deliver committed outbox events at least once; consumers deduplicate on ``event_id``.

Claim, publish and the result update are separate steps: the claim commits a lease, the
publisher runs with no transaction open, and the result is written by compare-and-set on
``lease_generation`` so a worker whose lease was reclaimed cannot overwrite it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from payment_module.application.config import PaymentModuleConfig, retry_delay
from payment_module.domain.enums import OutboxStatus
from payment_module.ports.clock import Clock
from payment_module.ports.publisher import OutboxPublisher
from payment_module.ports.unit_of_work import ClaimedOutbox, UnitOfWorkFactory

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """``status`` is what was recorded, or ``None`` when the lease had been reclaimed."""

    event_id: UUID
    status: OutboxStatus | None


class DispatchOutbox:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        publisher: OutboxPublisher,
        clock: Clock,
        config: PaymentModuleConfig,
    ) -> None:
        self._uow_factory = uow_factory
        self._publisher = publisher
        self._clock = clock
        self._config = config
        self._owner = config.owner()

    async def run_batch(self, limit: int = 50) -> list[DispatchResult]:
        async with self._uow_factory() as uow:
            claimed = await uow.outbox.claim_batch(
                limit, self._config.lease_seconds, self._owner, self._clock.now()
            )
            await uow.commit()
        return [await self._deliver(item) for item in claimed]

    async def _deliver(self, claimed: ClaimedOutbox) -> DispatchResult:
        event_id = claimed.event.event_id
        next_attempt_at = None
        error = None
        try:
            await self._publisher.publish(claimed.event)
            status = OutboxStatus.PUBLISHED
        except Exception as exc:
            error = type(exc).__name__
            if claimed.attempts >= self._config.outbox_max_attempts:
                status = OutboxStatus.FAILED
            else:
                status = OutboxStatus.PENDING
                next_attempt_at = self._clock.now() + retry_delay(claimed.attempts)
            logger.warning(
                "payment_outbox_publish_failed",
                extra={"event_id": str(event_id), "error": error, "attempts": claimed.attempts},
            )
        async with self._uow_factory() as uow:
            recorded = await uow.outbox.finalize(
                event_id,
                claimed.lease_generation,
                status,
                now=self._clock.now(),
                next_attempt_at=next_attempt_at,
                last_error=error,
            )
            await uow.commit()
        if not recorded:
            logger.warning("payment_outbox_lease_lost", extra={"event_id": str(event_id)})
            return DispatchResult(event_id, None)
        return DispatchResult(event_id, status)


class RequeueOutbox:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, event_id: UUID, actor: str, reason: str) -> bool:
        """``failed -> pending`` with attempts reset; ``False`` for any other state."""
        async with self._uow_factory() as uow:
            requeued = await uow.outbox.requeue(event_id)
            await uow.commit()
        logger.info(
            "payment_outbox_requeued" if requeued else "payment_outbox_requeue_refused",
            extra={"event_id": str(event_id), "actor": actor, "reason": reason},
        )
        return requeued
