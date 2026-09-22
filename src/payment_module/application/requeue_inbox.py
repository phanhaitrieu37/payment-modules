"""Operator step: send a failed or quarantined delivery back to the worker."""

from __future__ import annotations

import logging
from uuid import UUID

from payment_module.ports.unit_of_work import UnitOfWorkFactory

logger = logging.getLogger(__name__)


class RequeueInbox:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, inbox_id: UUID, actor: str, reason: str) -> bool:
        """``failed | quarantined -> received`` with attempts reset.

        Returns ``False`` when the row is in any other state. The host checks the actor's
        permission first; ``actor`` and ``reason`` are written to the audit log.
        """
        async with self._uow_factory() as uow:
            requeued = await uow.inbox.requeue(inbox_id)
            await uow.commit()
        logger.info(
            "payment_inbox_requeued" if requeued else "payment_inbox_requeue_refused",
            extra={"inbox_id": str(inbox_id), "actor": actor, "reason": reason},
        )
        return requeued
