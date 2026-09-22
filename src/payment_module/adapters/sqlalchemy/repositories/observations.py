"""Provider observations: every sighting of a transaction, with its provenance."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert as pg_insert

from payment_module.adapters.sqlalchemy.repositories import SqlAlchemyRepository
from payment_module.domain.enums import (
    Direction,
    Environment,
    LinkMethod,
    LinkStatus,
    ObservationSource,
)
from payment_module.domain.events import JsonValue
from payment_module.domain.money import AmountVnd


class SqlAlchemyObservationRepository(SqlAlchemyRepository):
    async def add(
        self,
        *,
        tenant_id: str,
        environment: Environment,
        connection_id: UUID,
        provider: str,
        source: ObservationSource,
        source_tx_id: str,
        reported_account_key: str,
        amount: AmountVnd,
        direction: Direction,
        observed_at: datetime,
        inbox_id: UUID | None = None,
        reconciliation_run_id: UUID | None = None,
        bank_reference: str | None = None,
        occurred_at: datetime | None = None,
        code: str | None = None,
        memo: str | None = None,
        normalized: Mapping[str, JsonValue] | None = None,
        transaction_id: UUID | None = None,
        link_method: LinkMethod | None = None,
        link_status: LinkStatus = LinkStatus.UNLINKED,
    ) -> UUID:
        observation_id = uuid.uuid4()
        await self._session.execute(
            self._tables.provider_observations.insert().values(
                id=observation_id,
                tenant_id=tenant_id,
                environment=Environment(environment).value,
                connection_id=connection_id,
                provider=provider,
                source=ObservationSource(source).value,
                source_tx_id=source_tx_id,
                inbox_id=inbox_id,
                reconciliation_run_id=reconciliation_run_id,
                reported_account_key=reported_account_key,
                bank_reference=bank_reference,
                amount_vnd=amount.value,
                direction=Direction(direction).value,
                occurred_at=occurred_at,
                code=code,
                memo=memo,
                normalized=None if normalized is None else dict(normalized),
                transaction_id=transaction_id,
                link_method=None if link_method is None else LinkMethod(link_method).value,
                link_status=LinkStatus(link_status).value,
                observed_at=observed_at,
            )
        )
        return observation_id

    async def add_if_absent(
        self,
        *,
        tenant_id: str,
        environment: Environment,
        connection_id: UUID,
        provider: str,
        source: ObservationSource,
        source_tx_id: str,
        reported_account_key: str,
        amount: AmountVnd,
        direction: Direction,
        observed_at: datetime,
        inbox_id: UUID | None = None,
        reconciliation_run_id: UUID | None = None,
        bank_reference: str | None = None,
        occurred_at: datetime | None = None,
        code: str | None = None,
        memo: str | None = None,
        transaction_id: UUID | None = None,
        link_method: LinkMethod | None = None,
        link_status: LinkStatus = LinkStatus.UNLINKED,
        purge_after: datetime | None = None,
    ) -> UUID | None:
        """Insert an observation, or ``None`` when this source id is already stored.

        Only the source unique key is a tolerated conflict (``ON CONFLICT`` on exactly its
        columns), so the transaction stays usable and other violations still raise.
        """
        t = self._tables.provider_observations
        return await self._session.scalar(
            pg_insert(t)
            .values(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                environment=Environment(environment).value,
                connection_id=connection_id,
                provider=provider,
                source=ObservationSource(source).value,
                source_tx_id=source_tx_id,
                inbox_id=inbox_id,
                reconciliation_run_id=reconciliation_run_id,
                reported_account_key=reported_account_key,
                bank_reference=bank_reference,
                amount_vnd=amount.value,
                direction=Direction(direction).value,
                occurred_at=occurred_at,
                code=code,
                memo=memo,
                transaction_id=transaction_id,
                link_method=None if link_method is None else LinkMethod(link_method).value,
                link_status=LinkStatus(link_status).value,
                observed_at=observed_at,
                purge_after=purge_after,
            )
            .on_conflict_do_nothing(
                index_elements=[t.c.provider, t.c.source, t.c.source_tx_id, t.c.connection_id]
            )
            .returning(t.c.id)
        )
