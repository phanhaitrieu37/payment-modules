"""One repository per aggregate; every repository shares the unit of work's session.

Repositories never log or ``repr`` full account numbers or raw webhook bodies.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from payment_module.adapters.sqlalchemy.tables import PaymentTables


class SqlAlchemyRepository:
    def __init__(self, session: AsyncSession, tables: PaymentTables) -> None:
        self._session = session
        self._tables = tables
