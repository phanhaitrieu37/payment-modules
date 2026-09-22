"""SQLAlchemy unit of work: one ``AsyncSession`` transaction shared by every repository.

``SqlAlchemyUnitOfWork(session_factory, tables)`` owns its session: ``commit`` and
``rollback`` end the transaction. ``SqlAlchemyUnitOfWork.joined(session, tables)`` works
inside a session the host owns, so an intent can be created or cancelled atomically with the
host's own rows: it runs in a savepoint, ``commit`` only flushes and releases the savepoint,
``rollback`` undoes the package's writes, and the host alone commits or rolls back the
outer transaction.

``.session`` is exposed for handlers that must write in the same transaction; that is a
contract of this adapter, not of the ``UnitOfWork`` port.
"""

from __future__ import annotations

from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction, async_sessionmaker

from payment_module.adapters.sqlalchemy.repositories.bindings import (
    SqlAlchemyConnectionBindingRepository,
)
from payment_module.adapters.sqlalchemy.repositories.connections import (
    SqlAlchemyConnectionRepository,
)
from payment_module.adapters.sqlalchemy.repositories.inbox import SqlAlchemyInboxRepository
from payment_module.adapters.sqlalchemy.repositories.intents import SqlAlchemyIntentRepository
from payment_module.adapters.sqlalchemy.repositories.merchants import (
    SqlAlchemyMerchantRepository,
)
from payment_module.adapters.sqlalchemy.repositories.observations import (
    SqlAlchemyObservationRepository,
)
from payment_module.adapters.sqlalchemy.repositories.outbox import SqlAlchemyOutboxRepository
from payment_module.adapters.sqlalchemy.repositories.readiness import (
    SqlAlchemyReadinessRepository,
)
from payment_module.adapters.sqlalchemy.repositories.receiving_accounts import (
    SqlAlchemyReceivingAccountRepository,
)
from payment_module.adapters.sqlalchemy.repositories.reconciliation_runs import (
    SqlAlchemyReconciliationRunRepository,
)
from payment_module.adapters.sqlalchemy.repositories.reference_profiles import (
    SqlAlchemyReferenceProfileRepository,
)
from payment_module.adapters.sqlalchemy.repositories.review_cases import (
    SqlAlchemyReviewCaseRepository,
)
from payment_module.adapters.sqlalchemy.repositories.settlements import (
    SqlAlchemySettlementRepository,
)
from payment_module.adapters.sqlalchemy.repositories.transactions import (
    SqlAlchemyTransactionRepository,
)
from payment_module.adapters.sqlalchemy.tables import PaymentTables


class SqlAlchemyUnitOfWork:
    """Use as ``async with uow: ...; await uow.commit()``; leaving without commit rolls back."""

    merchants: SqlAlchemyMerchantRepository
    receiving_accounts: SqlAlchemyReceivingAccountRepository
    connections: SqlAlchemyConnectionRepository
    connection_bindings: SqlAlchemyConnectionBindingRepository
    reference_profiles: SqlAlchemyReferenceProfileRepository
    readiness: SqlAlchemyReadinessRepository
    intents: SqlAlchemyIntentRepository
    inbox: SqlAlchemyInboxRepository
    observations: SqlAlchemyObservationRepository
    transactions: SqlAlchemyTransactionRepository
    settlements: SqlAlchemySettlementRepository
    review_cases: SqlAlchemyReviewCaseRepository
    outbox: SqlAlchemyOutboxRepository
    reconciliation_runs: SqlAlchemyReconciliationRunRepository

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], tables: PaymentTables
    ) -> None:
        self._session_factory: async_sessionmaker[AsyncSession] | None = session_factory
        self._tables = tables
        self._session: AsyncSession | None = None
        self._savepoint: AsyncSessionTransaction | None = None
        self._host_session: AsyncSession | None = None

    @classmethod
    def joined(cls, session: AsyncSession, tables: PaymentTables) -> SqlAlchemyUnitOfWork:
        """A unit of work inside the host's session; the host owns commit and rollback."""
        uow = cls.__new__(cls)
        uow._session_factory = None
        uow._tables = tables
        uow._session = None
        uow._savepoint = None
        uow._host_session = session
        return uow

    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("the unit of work is not active; use 'async with'")
        return self._session

    @property
    def is_joined(self) -> bool:
        return self._host_session is not None

    async def __aenter__(self) -> Self:
        if self._host_session is not None:
            self._session = self._host_session
            self._savepoint = await self._session.begin_nested()
        else:
            assert self._session_factory is not None
            self._session = self._session_factory()
        self._bind_repositories(self._session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            await self.rollback()
        finally:
            if not self.is_joined and self._session is not None:
                await self._session.close()
            self._session = None

    async def commit(self) -> None:
        session = self.session
        if self.is_joined:
            await session.flush()
            if self._savepoint is not None and self._savepoint.is_active:
                await self._savepoint.commit()
            self._savepoint = None
        else:
            await session.commit()

    async def rollback(self) -> None:
        if self._session is None:
            return
        if self.is_joined:
            if self._savepoint is not None and self._savepoint.is_active:
                await self._savepoint.rollback()
            self._savepoint = None
        else:
            await self._session.rollback()

    def _bind_repositories(self, session: AsyncSession) -> None:
        t = self._tables
        self.merchants = SqlAlchemyMerchantRepository(session, t)
        self.receiving_accounts = SqlAlchemyReceivingAccountRepository(session, t)
        self.connections = SqlAlchemyConnectionRepository(session, t)
        self.connection_bindings = SqlAlchemyConnectionBindingRepository(session, t)
        self.reference_profiles = SqlAlchemyReferenceProfileRepository(session, t)
        self.readiness = SqlAlchemyReadinessRepository(session, t)
        self.intents = SqlAlchemyIntentRepository(session, t)
        self.inbox = SqlAlchemyInboxRepository(session, t)
        self.observations = SqlAlchemyObservationRepository(session, t)
        self.transactions = SqlAlchemyTransactionRepository(session, t)
        self.settlements = SqlAlchemySettlementRepository(session, t)
        self.review_cases = SqlAlchemyReviewCaseRepository(session, t)
        self.outbox = SqlAlchemyOutboxRepository(session, t)
        self.reconciliation_runs = SqlAlchemyReconciliationRunRepository(session, t)


class SqlAlchemyUnitOfWorkFactory:
    """Creates a fresh owning unit of work per call."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], tables: PaymentTables
    ) -> None:
        self._session_factory = session_factory
        self._tables = tables

    def __call__(self) -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(self._session_factory, self._tables)
