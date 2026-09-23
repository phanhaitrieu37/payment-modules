"""Provider connections: one provider webhook of one merchant in one environment."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa

from payment_module.adapters.sqlalchemy.repositories import SqlAlchemyRepository
from payment_module.domain.enums import AuthMode, ConnectionStatus, Environment, ReconcileMode
from payment_module.ports.resolvers import ProviderConnection


class SqlAlchemyConnectionRepository(SqlAlchemyRepository):
    async def add(
        self,
        *,
        tenant_id: str,
        merchant_id: UUID,
        provider: str,
        environment: Environment,
        locator: str,
        secret_ref: str,
        api_credential_ref: str | None = None,
        auth_mode: AuthMode = AuthMode.HMAC,
    ) -> UUID:
        """New connections start ``pending`` in ``detect_only`` with the default tolerance."""
        connection_id = uuid.uuid4()
        await self._session.execute(
            self._tables.provider_connections.insert().values(
                id=connection_id,
                tenant_id=tenant_id,
                merchant_id=merchant_id,
                provider=provider,
                environment=Environment(environment).value,
                locator=locator,
                secret_ref=secret_ref,
                api_credential_ref=api_credential_ref,
                auth_mode=AuthMode(auth_mode).value,
            )
        )
        return connection_id

    async def get(self, tenant_id: str, connection_id: UUID) -> ProviderConnection | None:
        t = self._tables.provider_connections
        return await self._one(sa.and_(t.c.tenant_id == tenant_id, t.c.id == connection_id))

    async def get_for_update(
        self, tenant_id: str, connection_id: UUID
    ) -> ProviderConnection | None:
        t = self._tables.provider_connections
        row = (
            await self._session.execute(
                sa.select(t)
                .where(t.c.tenant_id == tenant_id, t.c.id == connection_id)
                .with_for_update()
            )
        ).first()
        return None if row is None else _connection(row)

    async def by_locator(self, locator: str) -> ProviderConnection | None:
        return await self._one(self._tables.provider_connections.c.locator == locator)

    async def list_active(self) -> Sequence[ProviderConnection]:
        t = self._tables.provider_connections
        rows = await self._session.execute(
            sa.select(t).where(t.c.status == ConnectionStatus.ACTIVE.value).order_by(t.c.id)
        )
        return [_connection(row) for row in rows]

    async def set_status(
        self,
        tenant_id: str,
        connection_id: UUID,
        status: ConnectionStatus,
        *,
        changed_by: str,
        changed_at: datetime,
        reason: str,
    ) -> bool:
        return await self._update(
            tenant_id,
            connection_id,
            status=ConnectionStatus(status).value,
            status_changed_by=changed_by,
            status_changed_at=changed_at,
            status_changed_reason=reason,
        )

    async def set_reconcile_mode(
        self,
        tenant_id: str,
        connection_id: UUID,
        mode: ReconcileMode,
        evidence_ref: str | None,
    ) -> bool:
        """The database rejects ``auto_settle`` without an evidence reference."""
        return await self._update(
            tenant_id,
            connection_id,
            reconcile_mode=ReconcileMode(mode).value,
            reconcile_evidence_ref=evidence_ref,
        )

    async def _update(self, tenant_id: str, connection_id: UUID, **values: object) -> bool:
        t = self._tables.provider_connections
        result = await self._session.execute(
            sa.update(t).where(t.c.tenant_id == tenant_id, t.c.id == connection_id).values(**values)
        )
        return result.rowcount == 1

    async def _one(self, condition: sa.ColumnElement[bool]) -> ProviderConnection | None:
        t = self._tables.provider_connections
        row = (await self._session.execute(sa.select(t).where(condition))).first()
        return None if row is None else _connection(row)

    async def list_reconcilable(self) -> Sequence[ProviderConnection]:
        """Connections of every tenant with an API credential that are not ``disabled``."""
        t = self._tables.provider_connections
        rows = await self._session.execute(
            sa.select(t)
            .where(
                t.c.api_credential_ref.is_not(None),
                t.c.status != ConnectionStatus.DISABLED.value,
            )
            .order_by(t.c.id)
        )
        return [_connection(row) for row in rows]


def _connection(row: sa.Row) -> ProviderConnection:
    return ProviderConnection(
        id=row.id,
        tenant_id=row.tenant_id,
        merchant_id=row.merchant_id,
        environment=row.environment,
        provider=row.provider,
        locator=row.locator,
        status=row.status,
        reconcile_mode=row.reconcile_mode,
        timestamp_tolerance_seconds=row.timestamp_tolerance_seconds,
        secret_ref=row.secret_ref,
        api_credential_ref=row.api_credential_ref,
    )
