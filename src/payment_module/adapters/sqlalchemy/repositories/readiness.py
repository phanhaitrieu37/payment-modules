"""Readiness evidence per connection x profile version x environment."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from uuid import UUID

import sqlalchemy as sa

from payment_module.adapters.sqlalchemy.repositories import SqlAlchemyRepository
from payment_module.domain.enums import Environment, ReadinessStatus
from payment_module.domain.events import JsonValue


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
        t = self._tables.connection_reference_readiness
        status = await self._session.scalar(
            sa.select(t.c.status).where(
                t.c.connection_id == connection_id,
                t.c.profile_version == profile_version,
                t.c.environment == Environment(environment).value,
            )
        )
        return None if status is None else ReadinessStatus(status)
