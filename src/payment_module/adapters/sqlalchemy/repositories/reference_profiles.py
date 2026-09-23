"""Project-level reference profiles and their named prefixes, written together."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import datetime

import sqlalchemy as sa

from payment_module.adapters.sqlalchemy.repositories import SqlAlchemyRepository
from payment_module.domain.enums import ProfileStatus
from payment_module.domain.reference import NamedPrefix, ReferenceProfile


class SqlAlchemyReferenceProfileRepository(SqlAlchemyRepository):
    async def add(self, profile: ReferenceProfile) -> None:
        """Insert the profile row and all its prefixes in the unit of work's transaction."""
        await self._session.execute(
            self._tables.reference_profiles.insert().values(
                version=profile.version,
                kind=profile.kind.value,
                suffix_length=profile.suffix_length,
                alphabet=profile.alphabet,
                status=profile.status.value,
            )
        )
        if profile.prefixes:
            await self._session.execute(
                self._tables.reference_profile_prefixes.insert(),
                [
                    {"profile_version": profile.version, "name": p.name, "prefix": p.prefix}
                    for p in profile.prefixes
                ],
            )

    async def get(self, version: int) -> ReferenceProfile | None:
        profiles = self._tables.reference_profiles
        row = (
            await self._session.execute(sa.select(profiles).where(profiles.c.version == version))
        ).first()
        return None if row is None else await self._profile(row)

    async def get_active(self) -> ReferenceProfile | None:
        """The one ``active`` profile (a partial unique index allows at most one)."""
        profiles = self._tables.reference_profiles
        version = await self._session.scalar(
            sa.select(profiles.c.version).where(profiles.c.status == ProfileStatus.ACTIVE.value)
        )
        return None if version is None else await self.get(version)

    async def lock_all(self) -> Sequence[ReferenceProfile]:
        return await self._all(sa.select(self._tables.reference_profiles).with_for_update())

    async def share_all(self) -> Sequence[ReferenceProfile]:
        return await self._all(
            sa.select(self._tables.reference_profiles).with_for_update(read=True)
        )

    async def set_status(
        self, version: int, status: ProfileStatus, *, actor: str, at: datetime
    ) -> None:
        t = self._tables.reference_profiles
        status = ProfileStatus(status)
        values: dict[str, object] = {"status": status.value}
        if status == ProfileStatus.ACTIVE:
            values |= {"activated_by": actor, "activated_at": at}
        elif status == ProfileStatus.RETIRED:
            values |= {"retired_by": actor, "retired_at": at}
        await self._session.execute(sa.update(t).where(t.c.version == version).values(**values))

    async def list_by_status(self, statuses: Collection[ProfileStatus]) -> list[ReferenceProfile]:
        t = self._tables.reference_profiles
        return await self._all(
            sa.select(t).where(t.c.status.in_([ProfileStatus(s).value for s in statuses]))
        )

    async def _all(self, query: sa.Select) -> list[ReferenceProfile]:
        profiles = self._tables.reference_profiles
        rows = (await self._session.execute(query.order_by(profiles.c.version))).all()
        return [await self._profile(row) for row in rows]

    async def _profile(self, row: sa.Row) -> ReferenceProfile:
        prefixes = self._tables.reference_profile_prefixes
        named = await self._session.execute(
            sa.select(prefixes.c.name, prefixes.c.prefix)
            .where(prefixes.c.profile_version == row.version)
            .order_by(prefixes.c.name)
        )
        return ReferenceProfile(
            version=row.version,
            prefixes=tuple(NamedPrefix(name, prefix) for name, prefix in named),
            suffix_length=row.suffix_length,
            alphabet=row.alphabet,
            kind=row.kind,
            status=row.status,
        )
