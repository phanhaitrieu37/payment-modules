"""Connection-to-account bindings, pinned to one merchant and one environment."""

from __future__ import annotations

from uuid import UUID

import sqlalchemy as sa

from payment_module.adapters.sqlalchemy.repositories import SqlAlchemyRepository
from payment_module.domain.enums import Environment


class SqlAlchemyConnectionBindingRepository(SqlAlchemyRepository):
    async def add(
        self,
        *,
        tenant_id: str,
        merchant_id: UUID,
        environment: Environment,
        connection_id: UUID,
        receiving_account_id: UUID,
        created_by: str,
    ) -> None:
        await self._session.execute(
            self._tables.connection_account_bindings.insert().values(
                tenant_id=tenant_id,
                merchant_id=merchant_id,
                environment=Environment(environment).value,
                connection_id=connection_id,
                receiving_account_id=receiving_account_id,
                created_by=created_by,
            )
        )

    async def delete_for_account(self, receiving_account_id: UUID) -> list[UUID]:
        t = self._tables.connection_account_bindings
        result = await self._session.scalars(
            sa.delete(t)
            .where(t.c.receiving_account_id == receiving_account_id)
            .returning(t.c.connection_id)
        )
        return sorted(result)

    async def account_ids(self, connection_id: UUID) -> list[UUID]:
        t = self._tables.connection_account_bindings
        result = await self._session.scalars(
            sa.select(t.c.receiving_account_id)
            .where(t.c.connection_id == connection_id)
            .order_by(t.c.receiving_account_id)
        )
        return list(result)

    async def connection_ids_for_account(self, receiving_account_id: UUID) -> list[UUID]:
        t = self._tables.connection_account_bindings
        result = await self._session.scalars(
            sa.select(t.c.connection_id)
            .where(t.c.receiving_account_id == receiving_account_id)
            .order_by(t.c.connection_id)
        )
        return list(result)
