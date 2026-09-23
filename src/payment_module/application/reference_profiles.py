"""Reference profile lifecycle: create a draft, activate it, retire old versions, import
legacy codes.

Profiles are project-level and immutable. Activation is strict: every ``active`` connection
of every tenant must already be ready for the candidate in its environment, otherwise it is
refused with the list of connections; an operator can move one to ``not_ready`` (audited) to
go ahead without it. The old active version and the candidate change status in one
transaction under a lock on every profile row, so there is never zero or two active
versions, even with concurrent activations. The old version stays ``accepted_legacy``: its
codes still match and its prefixes stay in every checklist until it is retired.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta

from payment_module.application.readiness import PROFILE_NOT_FOUND, require_actor
from payment_module.domain.enums import Environment, ProfileKind, ProfileStatus
from payment_module.domain.errors import (
    InvalidReferenceProfile,
    ProfileActivationBlocked,
    ReferenceProfileRejected,
)
from payment_module.domain.reference import (
    CrossVersionOverlap,
    NamedPrefix,
    ReferenceProfile,
    check_prefix_overlap,
)
from payment_module.ports.clock import Clock
from payment_module.ports.template_checklist import ReferenceTemplateChecklist
from payment_module.ports.unit_of_work import UnitOfWorkFactory

logger = logging.getLogger(__name__)

VERSION_EXISTS = "VERSION_EXISTS"
PROFILE_NOT_DRAFT = "PROFILE_NOT_DRAFT"
PROFILE_NOT_ACCEPTED_LEGACY = "PROFILE_NOT_ACCEPTED_LEGACY"
PROFILE_STILL_IN_USE = "PROFILE_STILL_IN_USE"

_ACCEPTED = (ProfileStatus.ACTIVE, ProfileStatus.ACCEPTED_LEGACY)


@dataclass(frozen=True, slots=True)
class CreatedProfile:
    """``advisories`` list candidate prefixes equal to or nested with a prefix of another
    accepted version: allowed, but the provider template must cover both code lengths."""

    profile: ReferenceProfile
    advisories: tuple[CrossVersionOverlap, ...]


class CreateReferenceProfile:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        template_checklists: Mapping[str, ReferenceTemplateChecklist],
    ) -> None:
        self._uow_factory = uow_factory
        self._checklists = template_checklists

    async def execute(
        self,
        version: int,
        prefixes: Mapping[str, str],
        suffix_length: int,
        alphabet: str,
        actor: str,
    ) -> CreatedProfile:
        """Create a ``draft`` generated profile from ``{name: prefix}``.

        Equal or nested prefixes inside the version raise ``PrefixOverlap``; a prefix outside
        2-5 letters ``A-Z`` or a limit a registered provider cannot recognize raises
        ``InvalidReferenceProfile``. Overlap with other versions is only reported.
        """
        require_actor(actor)
        profile = ReferenceProfile(
            version=version,
            prefixes=tuple(NamedPrefix(name, prefix) for name, prefix in prefixes.items()),
            suffix_length=suffix_length,
            alphabet=alphabet,
            kind=ProfileKind.GENERATED,
            status=ProfileStatus.DRAFT,
        )
        for provider, checklist in sorted(self._checklists.items()):
            issues = checklist.validate(profile)
            if issues:
                detail = "; ".join(f"{issue.code}: {issue.message}" for issue in issues)
                raise InvalidReferenceProfile(f"{provider} cannot recognize the profile: {detail}")
        async with self._uow_factory() as uow:
            if await uow.reference_profiles.get(version) is not None:
                raise ReferenceProfileRejected(VERSION_EXISTS)
            accepted = await uow.reference_profiles.list_by_status(_ACCEPTED)
            shapes = [shape for other in accepted for shape in other.shapes()]
            report = check_prefix_overlap(profile.prefixes, shapes)
            await uow.reference_profiles.add(profile)
            await uow.commit()
        logger.info(
            "payment_profile_created",
            extra={
                "profile_version": version,
                "actor": actor,
                "advisories": len(report.advisories),
            },
        )
        return CreatedProfile(profile, tuple(report.advisories))


class ActivateReferenceProfile:
    def __init__(self, uow_factory: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    async def execute(self, version: int, actor: str) -> ReferenceProfile:
        """``draft`` candidate -> ``active``, previous active -> ``accepted_legacy``.

        Raises :class:`ProfileActivationBlocked` listing every ``active`` connection that
        has no ``ready`` readiness for the candidate in its environment.
        """
        require_actor(actor)
        async with self._uow_factory() as uow:
            profiles = await uow.reference_profiles.lock_all()
            candidate = next((p for p in profiles if p.version == version), None)
            if candidate is None:
                raise ReferenceProfileRejected(PROFILE_NOT_FOUND)
            if candidate.kind != ProfileKind.GENERATED or candidate.status != ProfileStatus.DRAFT:
                raise ReferenceProfileRejected(PROFILE_NOT_DRAFT)
            blocked = [
                connection_id
                for environment in Environment
                for connection_id in await uow.readiness.list_missing_for(version, environment)
            ]
            if blocked:
                raise ProfileActivationBlocked(tuple(sorted(blocked)))
            now = self._clock.now()
            previous = next((p for p in profiles if p.status == ProfileStatus.ACTIVE), None)
            if previous is not None:
                await uow.reference_profiles.set_status(
                    previous.version, ProfileStatus.ACCEPTED_LEGACY, actor=actor, at=now
                )
            await uow.reference_profiles.set_status(
                version, ProfileStatus.ACTIVE, actor=actor, at=now
            )
            # The candidate's prefixes join every other draft's checklist, so readiness
            # recorded for those drafts no longer covers what they would need.
            for other in profiles:
                if other.status == ProfileStatus.DRAFT and other.version != version:
                    await uow.readiness.invalidate_for_version(other.version)
            activated = await uow.reference_profiles.get(version)
            await uow.commit()
        logger.info(
            "payment_profile_rotated",
            extra={
                "profile_version": version,
                "previous_version": None if previous is None else previous.version,
                "actor": actor,
            },
        )
        assert activated is not None
        return activated


class RetireReferenceProfile:
    def __init__(
        self, uow_factory: UnitOfWorkFactory, clock: Clock, late_settlement_days: int
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._late_settlement = timedelta(days=late_settlement_days)

    async def execute(self, version: int, actor: str, reason: str) -> ReferenceProfile:
        """``accepted_legacy -> retired`` once late money can no longer settle any intent of
        the version: none awaits payment and none expired within ``late_settlement_days``.

        Its readiness rows are retired with it, so its prefixes leave later checklists and
        the provider filter for them may be removed.
        """
        require_actor(actor)
        async with self._uow_factory() as uow:
            profiles = await uow.reference_profiles.lock_all()
            profile = next((p for p in profiles if p.version == version), None)
            if profile is None:
                raise ReferenceProfileRejected(PROFILE_NOT_FOUND)
            if profile.status != ProfileStatus.ACCEPTED_LEGACY:
                raise ReferenceProfileRejected(PROFILE_NOT_ACCEPTED_LEGACY)
            now = self._clock.now()
            expired_after = now - self._late_settlement
            if await uow.intents.count_settleable_by_profile_version(version, expired_after):
                raise ReferenceProfileRejected(PROFILE_STILL_IN_USE)
            await uow.reference_profiles.set_status(
                version, ProfileStatus.RETIRED, actor=actor, at=now
            )
            await uow.readiness.retire_for_version(version)
            retired = await uow.reference_profiles.get(version)
            await uow.commit()
        logger.info(
            "payment_profile_retired",
            extra={"profile_version": version, "actor": actor, "reason": reason},
        )
        assert retired is not None
        return retired


class ImportLegacyReferenceProfile:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, version: int, actor: str) -> ReferenceProfile:
        """An ``accepted_legacy`` ``legacy_import`` profile at the host-chosen ``version``.

        It generates nothing; ``CreateIntent`` accepts an old host code through
        ``reference_override`` only together with this exact version.
        """
        require_actor(actor)
        profile = ReferenceProfile(
            version=version,
            prefixes=(),
            suffix_length=None,
            alphabet=None,
            kind=ProfileKind.LEGACY_IMPORT,
            status=ProfileStatus.ACCEPTED_LEGACY,
        )
        async with self._uow_factory() as uow:
            if await uow.reference_profiles.get(version) is not None:
                raise ReferenceProfileRejected(VERSION_EXISTS)
            await uow.reference_profiles.add(profile)
            await uow.commit()
        logger.info("payment_profile_imported", extra={"profile_version": version, "actor": actor})
        return profile
