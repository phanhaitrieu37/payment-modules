"""Loading what the matching chain needs for one fact, shared by every path that matches.

The caller holds the fact's row lock; candidate intents are locked here (by id) so the
lock order stays fact, then intents, then the review case.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from payment_module.domain.enums import Direction, ReceiptTimeSource
from payment_module.domain.intent import IntentView
from payment_module.domain.matching.invariant_guard import ConnectionView
from payment_module.domain.matching.match_transaction import MatchContext
from payment_module.domain.transaction import TransactionView
from payment_module.ports.provider import ReceivingAccountView
from payment_module.ports.resolvers import ProviderConnection
from payment_module.ports.unit_of_work import UnitOfWork


def connection_view(connection: ProviderConnection) -> ConnectionView:
    return ConnectionView(
        connection.id,
        connection.tenant_id,
        connection.merchant_id,
        connection.environment,
    )


async def resolve_receiver(
    uow: UnitOfWork, connection: ProviderConnection, reported_account_key: str
) -> ReceivingAccountView | None:
    """The registered account the payload names, if it belongs to the connection's tenant.

    An account of another merchant of the same tenant is still recorded, so the guard
    reports the fact as unbound; an account of another tenant is never linked.
    """
    account = await uow.receiving_accounts.find_by_fingerprint(
        connection.environment, reported_account_key
    )
    if account is None or account.tenant_id != connection.tenant_id:
        return None
    return account


async def load_match_context(
    uow: UnitOfWork,
    *,
    tx: TransactionView,
    connection: ProviderConnection,
    tokens: Sequence[str],
    effective_received_at: datetime,
    time_source: ReceiptTimeSource,
) -> tuple[MatchContext, dict[UUID, IntentView]]:
    """The match context of ``tx`` and its candidate intents by id, locked ``FOR UPDATE``.

    Intents are looked up only for incoming money into an account bound to the connection
    whose memo carries at least one token; otherwise the guard decides on its own.
    """
    bound = frozenset(await uow.connection_bindings.account_ids(connection.id))
    tokens = tuple(tokens)
    candidates: dict[str, IntentView] = {}
    if tx.direction == Direction.IN and tx.receiving_account_id in bound and tokens:
        for intent in await uow.intents.find_by_references_for_update(tokens):
            candidates[intent.payment_reference] = intent
    ctx = MatchContext(
        tx=tx,
        connection=connection_view(connection),
        bound_account_ids=bound,
        tokens=tokens,
        candidates=candidates,
        effective_received_at=effective_received_at,
        time_source=time_source,
    )
    return ctx, {intent.id: intent for intent in candidates.values()}
