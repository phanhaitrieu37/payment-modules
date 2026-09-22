"""Read the committed state of an intent; never writes."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from payment_module.domain.enums import IntentStatus
from payment_module.domain.errors import IntentNotFound
from payment_module.domain.intent import IntentView
from payment_module.ports.clock import Clock
from payment_module.ports.unit_of_work import UnitOfWorkFactory


@dataclass(frozen=True, slots=True)
class IntentStatusView:
    """``effective_status`` reads ``expired`` for an unpaid intent past ``expires_at`` even
    before the expiry job updates the row; ``intent.status`` is the stored value."""

    intent: IntentView
    effective_status: IntentStatus


class GetIntentStatus:
    def __init__(self, uow_factory: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    async def execute(self, tenant_id: str, intent_id: UUID) -> IntentStatusView:
        async with self._uow_factory() as uow:
            intent = await uow.intents.get(tenant_id, intent_id)
        if intent is None:
            raise IntentNotFound(f"intent {intent_id} not found")
        effective = intent.status
        if effective == IntentStatus.AWAITING_PAYMENT and self._clock.now() > intent.expires_at:
            effective = IntentStatus.EXPIRED
        return IntentStatusView(intent, effective)
