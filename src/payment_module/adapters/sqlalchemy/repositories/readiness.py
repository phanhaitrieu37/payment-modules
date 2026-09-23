"""Readiness evidence per connection x profile version x environment."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from payment_module.adapters.sqlalchemy.repositories import SqlAlchemyRepository
from payment_module.domain.enums import ConnectionStatus, Environment, ReadinessStatus
from payment_module.domain.events import JsonValue
from payment_module.ports.unit_of_work import ReadinessView


class SqlAlchemyReadinessRepository(SqlAlchemyRepository):
    async def add(
        self,
        *,
        tenant_id: str,
        connection_id: UUID,
        profile_version: int,
        environment: Environment,
        checklist: Mapping[str, JsonValue],
        status: ReadinessStatus = ReadinessStatus.PENDING,
    ) -> UUID:
        readiness_id = uuid.uuid4()
        await self._session.execute(
            self._tables.connection_reference_readiness.insert().values(
                id=readiness_id,
                tenant_id=tenant_id,
                connection_id=connection_id,
                profile_version=profile_version,
                environment=Environment(environment).value,
                status=ReadinessStatus(status).value,
                checklist=dict(checklist),
            )
        )
        return readiness_id

    async def get_status(
        self, connection_id: UUID, profile_version: int, environment: Environment
    ) -> ReadinessStatus | None:
        view = await self.get(connection_id, profile_version, environment)
        return None if view is None else view.status

    async def get(
        self, connection_id: UUID, profile_version: int, environment: Environment
    ) -> ReadinessView | None:
        t = self._tables.connection_reference_readiness
        row = (
            await self._session.execute(
                sa.select(t).where(
                    t.c.connection_id == connection_id,
                    t.c.profile_version == profile_version,
                    t.c.environment == Environment(environment).value,
                )
            )
        ).first()
        return None if row is None else _view(row)

    async def upsert(
        self,
        *,
        tenant_id: str,
        connection_id: UUID,
        profile_version: int,
        environment: Environment,
        status: ReadinessStatus,
        checklist: Mapping[str, JsonValue],
        evidence_ref: str | None,
        verified_by: str | None,
        verified_at: datetime | None,
    ) -> ReadinessView:
        """Insert, or replace on ``(connection_id, profile_version, environment)``."""
        t = self._tables.connection_reference_readiness
        values = {
            "status": ReadinessStatus(status).value,
            "checklist": dict(checklist),
            "evidence_ref": evidence_ref,
            "verified_by": verified_by,
            "verified_at": verified_at,
        }
        row = (
            await self._session.execute(
                pg_insert(t)
                .values(
                    id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    connection_id=connection_id,
                    profile_version=profile_version,
                    environment=Environment(environment).value,
                    **values,
                )
                .on_conflict_do_update(
                    index_elements=[t.c.connection_id, t.c.profile_version, t.c.environment],
                    set_=values,
                )
                .returning(t)
            )
        ).one()
        return _view(row)

    async def list_missing_for(self, version: int, environment: Environment) -> list[UUID]:
        c = self._tables.provider_connections
        r = self._tables.connection_reference_readiness
        ready = sa.exists().where(
            r.c.connection_id == c.c.id,
            r.c.profile_version == version,
            r.c.environment == c.c.environment,
            r.c.status == ReadinessStatus.READY.value,
        )
        result = await self._session.scalars(
            sa.select(c.c.id)
            .where(
                c.c.status == ConnectionStatus.ACTIVE.value,
                c.c.environment == Environment(environment).value,
                ~ready,
            )
            .order_by(c.c.id)
        )
        return list(result)

    async def invalidate_for_connection(self, connection_id: UUID) -> int:
        t = self._tables.connection_reference_readiness
        result = await self._session.execute(
            sa.update(t)
            .where(t.c.connection_id == connection_id, t.c.status == ReadinessStatus.READY.value)
            .values(status=ReadinessStatus.PENDING.value)
        )
        return result.rowcount

    async def invalidate_for_version(self, version: int) -> int:
        t = self._tables.connection_reference_readiness
        result = await self._session.execute(
            sa.update(t)
            .where(t.c.profile_version == version, t.c.status == ReadinessStatus.READY.value)
            .values(status=ReadinessStatus.PENDING.value)
        )
        return result.rowcount

    async def retire_for_version(self, version: int) -> int:
        t = self._tables.connection_reference_readiness
        result = await self._session.execute(
            sa.update(t)
            .where(t.c.profile_version == version, t.c.status != ReadinessStatus.RETIRED.value)
            .values(status=ReadinessStatus.RETIRED.value)
        )
        return result.rowcount


def _view(row: sa.Row) -> ReadinessView:
    checklist = row.checklist if isinstance(row.checklist, dict) else {}
    confirmed = checklist.get("confirmed")
    return ReadinessView(
        id=row.id,
        tenant_id=row.tenant_id,
        connection_id=row.connection_id,
        profile_version=row.profile_version,
        environment=row.environment,
        status=row.status,
        confirmed=frozenset(confirmed) if isinstance(confirmed, list) else frozenset(),
        evidence_ref=row.evidence_ref,
        verified_by=row.verified_by,
        verified_at=row.verified_at,
    )
