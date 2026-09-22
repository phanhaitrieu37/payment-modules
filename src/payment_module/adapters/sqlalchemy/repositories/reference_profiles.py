"""Project-level reference profiles and their named prefixes, written together."""

from __future__ import annotations

import sqlalchemy as sa

from payment_module.adapters.sqlalchemy.repositories import SqlAlchemyRepository
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
        prefixes = self._tables.reference_profile_prefixes
        row = (
            await self._session.execute(sa.select(profiles).where(profiles.c.version == version))
        ).first()
        if row is None:
            return None
        named = await self._session.execute(
            sa.select(prefixes.c.name, prefixes.c.prefix)
            .where(prefixes.c.profile_version == version)
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
