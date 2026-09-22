"""Receiving accounts: one owner (tenant + merchant) per account per environment."""

from __future__ import annotations

import uuid
from uuid import UUID

import sqlalchemy as sa

from payment_module.adapters.sqlalchemy.repositories import SqlAlchemyRepository
from payment_module.domain.enums import Environment, ReceivingAccountStatus
from payment_module.ports.provider import ReceivingAccountView


class SqlAlchemyReceivingAccountRepository(SqlAlchemyRepository):
    async def add(
        self,
        *,
        tenant_id: str,
        merchant_id: UUID,
        environment: Environment,
        bank_code: str,
        bank_bin: str | None,
        account_number: str,
        sub_account: str | None,
        account_number_masked: str,
        holder_name: str,
        account_fingerprint: str,
        provider_account_ref: str | None = None,
    ) -> UUID:
        """``account_fingerprint`` is computed by the application; storage does not derive it."""
        account_id = uuid.uuid4()
        await self._session.execute(
            self._tables.receiving_accounts.insert().values(
                id=account_id,
                tenant_id=tenant_id,
                merchant_id=merchant_id,
                environment=Environment(environment).value,
                bank_code=bank_code,
                bank_bin=bank_bin,
                account_number=account_number,
                sub_account=sub_account or "",
                account_number_masked=account_number_masked,
                holder_name=holder_name,
                account_fingerprint=account_fingerprint,
                provider_account_ref=provider_account_ref,
                status=ReceivingAccountStatus.ACTIVE.value,
            )
        )
        return account_id

    async def get(self, tenant_id: str, account_id: UUID) -> ReceivingAccountView | None:
        t = self._tables.receiving_accounts
        row = (
            await self._session.execute(
                sa.select(t).where(t.c.tenant_id == tenant_id, t.c.id == account_id)
            )
        ).first()
        return None if row is None else _view(row)

    async def find_by_fingerprint(
        self, environment: Environment, account_fingerprint: str
    ) -> ReceivingAccountView | None:
        t = self._tables.receiving_accounts
        row = (
            await self._session.execute(
                sa.select(t).where(
                    t.c.environment == Environment(environment).value,
                    t.c.account_fingerprint == account_fingerprint,
                )
            )
        ).first()
        return None if row is None else _view(row)


def _view(row: sa.Row) -> ReceivingAccountView:
    return ReceivingAccountView(
        id=row.id,
        tenant_id=row.tenant_id,
        merchant_id=row.merchant_id,
        environment=row.environment,
        bank_code=row.bank_code,
        account_number=row.account_number,
        sub_account=row.sub_account or None,
        account_name=row.holder_name,
        status=row.status,
    )
