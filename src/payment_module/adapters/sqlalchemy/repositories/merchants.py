"""Merchants: the receiving parties of one host tenant."""

from __future__ import annotations

import uuid
from uuid import UUID

import sqlalchemy as sa

from payment_module.adapters.sqlalchemy.repositories import SqlAlchemyRepository
from payment_module.domain.enums import MerchantStatus
from payment_module.ports.unit_of_work import MerchantView


class SqlAlchemyMerchantRepository(SqlAlchemyRepository):
    async def add(
        self,
        tenant_id: str,
        host_merchant_ref: str,
        status: MerchantStatus = MerchantStatus.ACTIVE,
    ) -> UUID:
        merchant_id = uuid.uuid4()
        await self._session.execute(
            self._tables.merchants.insert().values(
                id=merchant_id,
                tenant_id=tenant_id,
                host_merchant_ref=host_merchant_ref,
                status=MerchantStatus(status).value,
            )
        )
        return merchant_id

    async def get_id_by_host_ref(self, tenant_id: str, host_merchant_ref: str) -> UUID | None:
        t = self._tables.merchants
        return await self._session.scalar(
            sa.select(t.c.id).where(
                t.c.tenant_id == tenant_id, t.c.host_merchant_ref == host_merchant_ref
            )
        )

    async def get(self, tenant_id: str, merchant_id: UUID) -> MerchantView | None:
        t = self._tables.merchants
        row = (
            await self._session.execute(
                sa.select(t).where(t.c.tenant_id == tenant_id, t.c.id == merchant_id)
            )
        ).first()
        if row is None:
            return None
        return MerchantView(
            id=row.id,
            tenant_id=row.tenant_id,
            host_merchant_ref=row.host_merchant_ref,
            status=row.status,
        )
