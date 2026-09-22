"""Cancel an unpaid intent, or mark it superseded by the intent that replaces it."""

from __future__ import annotations

import dataclasses
import logging
from uuid import UUID

from payment_module.domain.enums import IntentStatus
from payment_module.domain.errors import IllegalTransition, IntentNotFound, IntentRejected
from payment_module.domain.intent import IntentView, transition_intent
from payment_module.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory

logger = logging.getLogger(__name__)

INVALID_SUPERSEDING_INTENT = "INVALID_SUPERSEDING_INTENT"


class CancelIntent:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(
        self,
        tenant_id: str,
        intent_id: UUID,
        reason: str,
        superseded_by_intent_id: UUID | None = None,
        uow: UnitOfWork | None = None,
    ) -> IntentView:
        """``superseded_by_intent_id`` given: ``superseded``; otherwise ``cancelled``.

        Only an ``awaiting_payment`` intent can be closed. The row is locked ``FOR UPDATE``,
        so a settlement racing this call either wins first (and this raises
        :class:`IllegalTransition`) or waits and then sees the intent closed.
        """
        work = uow if uow is not None else self._uow_factory()
        async with work:
            closed = await self._close(work, tenant_id, intent_id, reason, superseded_by_intent_id)
            await work.commit()
        logger.info(
            "payment_intent_closed",
            extra={"intent_id": str(intent_id), "status": closed.status.value},
        )
        return closed

    @staticmethod
    async def _close(
        uow: UnitOfWork,
        tenant_id: str,
        intent_id: UUID,
        reason: str,
        superseded_by: UUID | None,
    ) -> IntentView:
        intent = await uow.intents.get_for_update(tenant_id, intent_id)
        if intent is None:
            raise IntentNotFound(f"intent {intent_id} not found")
        target = IntentStatus.CANCELLED if superseded_by is None else IntentStatus.SUPERSEDED
        transition_intent(intent.status, target)
        if superseded_by is not None:
            # Checked here so a bad id is a rejection, not a foreign-key error that would
            # abort a joined host transaction.
            successor = await uow.intents.get(tenant_id, superseded_by)
            if (
                successor is None
                or successor.id == intent.id
                or successor.merchant_id != intent.merchant_id
                or successor.environment != intent.environment
            ):
                raise IntentRejected(INVALID_SUPERSEDING_INTENT)
        updated = await uow.intents.set_status(
            tenant_id,
            intent_id,
            target,
            cancel_reason=reason,
            superseded_by_intent_id=superseded_by,
        )
        if not updated:
            raise IllegalTransition("payment_intent", intent.status.value, target.value)
        return dataclasses.replace(intent, status=target, superseded_by_intent_id=superseded_by)
