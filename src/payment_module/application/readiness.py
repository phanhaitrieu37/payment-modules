"""Readiness of a connection for one reference profile version, and what invalidates it.

Readiness is manual evidence that the provider side of one connection (company-level code
template, webhook filters, URL, secret, bound accounts) matches one profile version in one
environment. The checklist a version needs covers every prefix of that version **and** of
every other version still accepted, so late money carrying an old code is still delivered.

A connection only becomes ``active`` with current readiness for the active profile. Any
change that could make the provider setup disagree (a binding added or removed, an account
retired) invalidates it in the same transaction: the connection drops to ``not_ready`` and
``detect_only``, and every ``ready`` row goes back to ``pending``.

Lock order: profiles (``FOR SHARE`` or ``FOR UPDATE``) before the connection row, on every
path that can make a connection ``active``, so it never interleaves with an activation.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from payment_module.domain.enums import (
    ConnectionStatus,
    Environment,
    ProfileKind,
    ProfileStatus,
    ReadinessStatus,
    ReceivingAccountStatus,
    ReconcileMode,
)
from payment_module.domain.errors import (
    OnboardingRejected,
    ReadinessChecklistIncomplete,
    ReferenceProfileRejected,
)
from payment_module.domain.reference import PrefixShape, ReferenceProfile
from payment_module.ports.clock import Clock
from payment_module.ports.resolvers import ProviderConnection
from payment_module.ports.template_checklist import Checklist, ReferenceTemplateChecklist
from payment_module.ports.unit_of_work import ReadinessView, UnitOfWork, UnitOfWorkFactory

logger = logging.getLogger(__name__)

CONNECTION_NOT_FOUND = "CONNECTION_NOT_FOUND"
ENVIRONMENT_MISMATCH = "ENVIRONMENT_MISMATCH"
EVIDENCE_REQUIRED = "EVIDENCE_REQUIRED"
ACTOR_REQUIRED = "ACTOR_REQUIRED"
PROFILE_NOT_FOUND = "PROFILE_NOT_FOUND"
PROFILE_NOT_GENERATED = "PROFILE_NOT_GENERATED"
PROFILE_RETIRED = "PROFILE_RETIRED"

TEMPLATE_CHECKLIST_ITEM = "template_checklist"
"""Reported as missing when no template checklist is registered for the provider."""

ACCOUNTS_BOUND_ITEM = "accounts:bound"
"""Derived from storage, never from the operator's checklist: at least one active
receiving account of the connection's merchant and environment is bound to it."""

_ACCEPTED = frozenset({ProfileStatus.ACTIVE, ProfileStatus.ACCEPTED_LEGACY})


@dataclass(frozen=True, slots=True)
class ReadinessResult:
    readiness: ReadinessView
    connection_status: ConnectionStatus


def require_actor(actor: str) -> str:
    if not isinstance(actor, str) or not actor.strip():
        raise OnboardingRejected(ACTOR_REQUIRED)
    return actor


def accepted_shapes(
    profiles: Sequence[ReferenceProfile], candidate: ReferenceProfile
) -> tuple[PrefixShape, ...]:
    """Code shapes of every other generated version still accepted (active or legacy)."""
    return tuple(
        shape
        for profile in profiles
        if profile.version != candidate.version
        and profile.kind == ProfileKind.GENERATED
        and profile.status in _ACCEPTED
        for shape in profile.shapes()
    )


def required_keys(
    checklist: Checklist, candidate: ReferenceProfile, accepted: Sequence[PrefixShape]
) -> frozenset[str]:
    """The provider's items, the bound-account item, plus a template and a webhook filter
    for every prefix value of the candidate and of the accepted versions, whatever the
    provider returned."""
    keys = {item.key for item in checklist.items} | {ACCOUNTS_BOUND_ITEM}
    for prefix in {named.prefix for named in candidate.prefixes} | {s.prefix for s in accepted}:
        keys |= {f"template:{prefix}", f"webhook_filter:{prefix}"}
    return frozenset(keys)


def generated_profile(profiles: Sequence[ReferenceProfile], version: int) -> ReferenceProfile:
    """The generated, not retired profile ``version`` from ``profiles``."""
    profile = next((p for p in profiles if p.version == version), None)
    if profile is None:
        raise ReferenceProfileRejected(PROFILE_NOT_FOUND)
    if profile.kind != ProfileKind.GENERATED:
        raise ReferenceProfileRejected(PROFILE_NOT_GENERATED)
    if profile.status == ProfileStatus.RETIRED:
        raise ReferenceProfileRejected(PROFILE_RETIRED)
    return profile


async def invalidate_readiness(
    uow: UnitOfWork, connection: ProviderConnection, *, actor: str, reason: str, now: datetime
) -> None:
    """The provider setup may no longer match: ``not_ready``, ``detect_only``, readiness
    ``pending``, all in the caller's transaction."""
    await uow.readiness.invalidate_for_connection(connection.id)
    if connection.status == ConnectionStatus.ACTIVE:
        await uow.connections.set_status(
            connection.tenant_id,
            connection.id,
            ConnectionStatus.NOT_READY,
            changed_by=actor,
            changed_at=now,
            reason=reason,
        )
    await uow.connections.set_reconcile_mode(
        connection.tenant_id, connection.id, ReconcileMode.DETECT_ONLY, None
    )
    logger.info(
        "payment_connection_readiness_invalidated",
        extra={"connection_id": str(connection.id), "actor": actor, "reason": reason},
    )


async def has_active_binding(uow: UnitOfWork, connection: ProviderConnection) -> bool:
    """Whether an active account of the connection's own merchant and environment is bound."""
    for account_id in await uow.connection_bindings.account_ids(connection.id):
        account = await uow.receiving_accounts.get(connection.tenant_id, account_id)
        if (
            account is not None
            and account.status == ReceivingAccountStatus.ACTIVE
            and account.merchant_id == connection.merchant_id
            and account.environment == connection.environment
        ):
            return True
    return False


async def is_ready(uow: UnitOfWork, connection: ProviderConnection, version: int) -> bool:
    readiness = await uow.readiness.get(connection.id, version, connection.environment)
    return readiness is not None and readiness.status == ReadinessStatus.READY


class RecordConnectionReadiness:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        template_checklists: Mapping[str, ReferenceTemplateChecklist],
        clock: Clock,
    ) -> None:
        self._uow_factory = uow_factory
        self._checklists = template_checklists
        self._clock = clock

    async def execute(
        self,
        tenant_id: str,
        connection_id: UUID,
        profile_version: int,
        environment: Environment,
        checklist: Mapping[str, bool],
        evidence_ref: str,
        verified_by: str,
    ) -> ReadinessResult:
        """Record ``ready`` when every expected item is confirmed ``True``.

        ``accounts:bound`` is read from the current bindings; the flag in ``checklist`` is
        ignored, so a connection without an active bound account is never ready.

        A missing or unconfirmed item raises :class:`ReadinessChecklistIncomplete` listing
        the keys. When ``profile_version`` is the active profile, a ``pending`` or
        ``not_ready`` connection becomes ``active`` in the same transaction.
        """
        require_actor(verified_by)
        if not evidence_ref:
            raise OnboardingRejected(EVIDENCE_REQUIRED)
        async with self._uow_factory() as uow:
            profiles = await uow.reference_profiles.share_all()
            connection = await uow.connections.get_for_update(tenant_id, connection_id)
            if connection is None:
                raise OnboardingRejected(CONNECTION_NOT_FOUND)
            if Environment(environment) != connection.environment:
                raise OnboardingRejected(ENVIRONMENT_MISMATCH)
            profile = generated_profile(profiles, profile_version)
            confirmed = {key for key, done in checklist.items() if done is True}
            confirmed.discard(ACCOUNTS_BOUND_ITEM)
            if await has_active_binding(uow, connection):
                confirmed.add(ACCOUNTS_BOUND_ITEM)
            self._check_complete(connection, profile, profiles, confirmed)
            now = self._clock.now()
            readiness = await uow.readiness.upsert(
                tenant_id=tenant_id,
                connection_id=connection.id,
                profile_version=profile.version,
                environment=connection.environment,
                status=ReadinessStatus.READY,
                checklist={"confirmed": sorted(confirmed)},
                evidence_ref=evidence_ref,
                verified_by=verified_by,
                verified_at=now,
            )
            status = connection.status
            if profile.status == ProfileStatus.ACTIVE and status in (
                ConnectionStatus.PENDING,
                ConnectionStatus.NOT_READY,
            ):
                status = ConnectionStatus.ACTIVE
                await uow.connections.set_status(
                    tenant_id,
                    connection.id,
                    status,
                    changed_by=verified_by,
                    changed_at=now,
                    reason=f"readiness recorded for reference profile v{profile.version}",
                )
            await uow.commit()
        logger.info(
            "payment_connection_readiness_recorded",
            extra={
                "connection_id": str(connection_id),
                "profile_version": profile_version,
                "actor": verified_by,
                "status": status.value,
            },
        )
        return ReadinessResult(readiness, status)

    def _check_complete(
        self,
        connection: ProviderConnection,
        profile: ReferenceProfile,
        profiles: Sequence[ReferenceProfile],
        confirmed: Collection[str],
    ) -> None:
        template_checklist = self._checklists.get(connection.provider)
        if template_checklist is None:
            raise ReadinessChecklistIncomplete((TEMPLATE_CHECKLIST_ITEM,))
        accepted = accepted_shapes(profiles, profile)
        expected = template_checklist.checklist(connection, profile, accepted)
        missing = tuple(sorted(required_keys(expected, profile, accepted) - set(confirmed)))
        if missing:
            raise ReadinessChecklistIncomplete(missing)
