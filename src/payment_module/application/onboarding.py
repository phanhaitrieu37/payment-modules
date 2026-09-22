"""Merchant onboarding: merchants, receiving accounts, connections, bindings, their status.

Every operator step takes an ``actor``; the host checks permissions before calling. Scope
rules are pre-checked here so a rejection is a stable code, and the composite foreign keys
reject anything that slips through: an account and a connection bound together share tenant,
merchant and environment, and one bank account or virtual account has exactly one owner per
environment in the installation.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Mapping
from uuid import UUID

from payment_module.application.readiness import (
    CONNECTION_NOT_FOUND,
    has_active_binding,
    invalidate_readiness,
    is_ready,
    require_actor,
)
from payment_module.domain.account_identity import account_key
from payment_module.domain.enums import (
    ConnectionStatus,
    Environment,
    MerchantStatus,
    ProfileStatus,
    ReceivingAccountStatus,
    ReconcileMode,
)
from payment_module.domain.errors import EvidenceRejected, IllegalTransition, OnboardingRejected
from payment_module.ports.clock import Clock
from payment_module.ports.evidence import NOT_FOUND, EvidenceVerifier
from payment_module.ports.provider import PaymentProvider, ReceivingAccountView
from payment_module.ports.resolvers import ProviderConnection
from payment_module.ports.unit_of_work import MerchantView, UnitOfWork, UnitOfWorkFactory

logger = logging.getLogger(__name__)

MERCHANT_NOT_FOUND = "MERCHANT_NOT_FOUND"
MERCHANT_INACTIVE = "MERCHANT_INACTIVE"
ACCOUNT_NOT_FOUND = "ACCOUNT_NOT_FOUND"
ACCOUNT_ALREADY_OWNED = "ACCOUNT_ALREADY_OWNED"
ACCOUNT_NOT_ACTIVE = "ACCOUNT_NOT_ACTIVE"
UNKNOWN_PROVIDER = "UNKNOWN_PROVIDER"
SCOPE_MISMATCH = "SCOPE_MISMATCH"
REASON_REQUIRED = "REASON_REQUIRED"
NO_ACTIVE_PROFILE = "NO_ACTIVE_PROFILE"
CONNECTION_NOT_READY = "CONNECTION_NOT_READY"
NO_BOUND_ACCOUNT = "NO_BOUND_ACCOUNT"

_ACCOUNT_TRANSITIONS: dict[ReceivingAccountStatus, frozenset[ReceivingAccountStatus]] = {
    ReceivingAccountStatus.ACTIVE: frozenset(
        {ReceivingAccountStatus.DISABLED, ReceivingAccountStatus.RETIRED}
    ),
    ReceivingAccountStatus.DISABLED: frozenset(
        {ReceivingAccountStatus.ACTIVE, ReceivingAccountStatus.RETIRED}
    ),
    ReceivingAccountStatus.RETIRED: frozenset(),
}
_CONNECTION_TRANSITIONS: dict[ConnectionStatus, frozenset[ConnectionStatus]] = {
    ConnectionStatus.PENDING: frozenset(
        {ConnectionStatus.ACTIVE, ConnectionStatus.NOT_READY, ConnectionStatus.DISABLED}
    ),
    ConnectionStatus.NOT_READY: frozenset({ConnectionStatus.ACTIVE, ConnectionStatus.DISABLED}),
    ConnectionStatus.ACTIVE: frozenset({ConnectionStatus.NOT_READY, ConnectionStatus.DISABLED}),
    ConnectionStatus.DISABLED: frozenset({ConnectionStatus.ACTIVE}),
}


def _require_reason(reason: str) -> str:
    if not isinstance(reason, str) or not reason.strip():
        raise OnboardingRejected(REASON_REQUIRED)
    return reason


def mask_account_number(account_number: str) -> str:
    return f"****{account_number.strip()[-4:]}"


async def _connection_for_update(
    uow: UnitOfWork, tenant_id: str, connection_id: UUID
) -> ProviderConnection:
    connection = await uow.connections.get_for_update(tenant_id, connection_id)
    if connection is None:
        raise OnboardingRejected(CONNECTION_NOT_FOUND)
    return connection


class RegisterMerchant:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, tenant_id: str, host_merchant_ref: str, actor: str) -> MerchantView:
        """Idempotent on ``host_merchant_ref``: a second call returns the same merchant."""
        require_actor(actor)
        async with self._uow_factory() as uow:
            merchant_id = await uow.merchants.get_id_by_host_ref(tenant_id, host_merchant_ref)
            if merchant_id is None:
                merchant_id = await uow.merchants.add(tenant_id, host_merchant_ref)
                logger.info(
                    "payment_merchant_registered",
                    extra={"merchant_id": str(merchant_id), "actor": actor},
                )
            merchant = await uow.merchants.get(tenant_id, merchant_id)
            await uow.commit()
        assert merchant is not None
        return merchant


class RegisterReceivingAccount:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(
        self,
        tenant_id: str,
        merchant_id: UUID,
        environment: Environment,
        bank_code: str,
        account_number: str,
        holder_name: str,
        actor: str,
        *,
        sub_account: str | None = None,
        bank_bin: str | None = None,
        provider_account_ref: str | None = None,
    ) -> ReceivingAccountView:
        """The fingerprint is ``account_key(bank, number, sub)``, the same key the provider
        adapter computes from payloads. An account already owned in ``environment`` by any
        tenant or merchant is refused with ``ACCOUNT_ALREADY_OWNED``."""
        require_actor(actor)
        fingerprint = account_key(bank_code, account_number, sub_account)
        async with self._uow_factory() as uow:
            merchant = await uow.merchants.get(tenant_id, merchant_id)
            if merchant is None:
                raise OnboardingRejected(MERCHANT_NOT_FOUND)
            if merchant.status != MerchantStatus.ACTIVE:
                raise OnboardingRejected(MERCHANT_INACTIVE)
            if await uow.receiving_accounts.find_by_fingerprint(environment, fingerprint):
                raise OnboardingRejected(ACCOUNT_ALREADY_OWNED)
            account_id = await uow.receiving_accounts.add(
                tenant_id=tenant_id,
                merchant_id=merchant_id,
                environment=environment,
                bank_code=bank_code.strip().upper(),
                bank_bin=bank_bin,
                account_number=account_number.strip(),
                sub_account=sub_account.strip() if sub_account else None,
                account_number_masked=mask_account_number(account_number),
                holder_name=holder_name,
                account_fingerprint=fingerprint,
                provider_account_ref=provider_account_ref,
            )
            account = await uow.receiving_accounts.get(tenant_id, account_id)
            await uow.commit()
        logger.info(
            "payment_account_registered", extra={"account_id": str(account_id), "actor": actor}
        )
        assert account is not None
        return account


class SetReceivingAccountStatus:
    def __init__(self, uow_factory: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    async def execute(
        self,
        tenant_id: str,
        account_id: UUID,
        status: ReceivingAccountStatus,
        actor: str,
        reason: str,
    ) -> ReceivingAccountView:
        """``disabled`` stops new intents but keeps bindings, so money for intents already
        issued still settles. ``retired`` removes every binding of the account and
        invalidates those connections' readiness in the same transaction, so later money
        into it goes to review as ``RECEIVER_UNBOUND``."""
        require_actor(actor)
        _require_reason(reason)
        target = ReceivingAccountStatus(status)
        async with self._uow_factory() as uow:
            account = await uow.receiving_accounts.get(tenant_id, account_id)
            if account is None:
                raise OnboardingRejected(ACCOUNT_NOT_FOUND)
            if target not in _ACCOUNT_TRANSITIONS[account.status]:
                raise IllegalTransition("receiving_account", account.status.value, target.value)
            await uow.receiving_accounts.set_status(tenant_id, account_id, target)
            if target == ReceivingAccountStatus.RETIRED:
                now = self._clock.now()
                for connection_id in await uow.connection_bindings.delete_for_account(account_id):
                    connection = await _connection_for_update(uow, tenant_id, connection_id)
                    await invalidate_readiness(
                        uow, connection, actor=actor, reason="bound account retired", now=now
                    )
            updated = await uow.receiving_accounts.get(tenant_id, account_id)
            await uow.commit()
        logger.info(
            "payment_account_status_changed",
            extra={
                "account_id": str(account_id),
                "status": target.value,
                "actor": actor,
                "reason": reason,
            },
        )
        assert updated is not None
        return updated


class RegisterConnection:
    def __init__(
        self, uow_factory: UnitOfWorkFactory, providers: Mapping[str, PaymentProvider]
    ) -> None:
        self._uow_factory = uow_factory
        self._providers = providers

    async def execute(
        self,
        tenant_id: str,
        merchant_id: UUID,
        provider: str,
        environment: Environment,
        secret_ref: str,
        actor: str,
        *,
        api_credential_ref: str | None = None,
        locator: str | None = None,
    ) -> ProviderConnection:
        """A new connection is ``pending`` in ``detect_only`` and takes no intents until its
        readiness for the active profile is recorded. ``locator`` defaults to 192 random
        bits, URL-safe."""
        require_actor(actor)
        if provider not in self._providers:
            raise OnboardingRejected(UNKNOWN_PROVIDER)
        async with self._uow_factory() as uow:
            if await uow.merchants.get(tenant_id, merchant_id) is None:
                raise OnboardingRejected(MERCHANT_NOT_FOUND)
            connection_id = await uow.connections.add(
                tenant_id=tenant_id,
                merchant_id=merchant_id,
                provider=provider,
                environment=Environment(environment),
                locator=locator or secrets.token_urlsafe(24),
                secret_ref=secret_ref,
                api_credential_ref=api_credential_ref,
            )
            connection = await uow.connections.get(tenant_id, connection_id)
            await uow.commit()
        logger.info(
            "payment_connection_registered",
            extra={"connection_id": str(connection_id), "actor": actor},
        )
        assert connection is not None
        return connection


class BindConnectionAccount:
    def __init__(self, uow_factory: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    async def execute(
        self, tenant_id: str, connection_id: UUID, receiving_account_id: UUID, actor: str
    ) -> ProviderConnection:
        """Bind an account of the connection's own merchant and environment.

        Anything else is ``SCOPE_MISMATCH``. A new binding invalidates the connection's
        readiness in the same transaction; binding an account already bound changes nothing.
        """
        require_actor(actor)
        async with self._uow_factory() as uow:
            connection = await _connection_for_update(uow, tenant_id, connection_id)
            account = await uow.receiving_accounts.get(tenant_id, receiving_account_id)
            if account is None:
                raise OnboardingRejected(ACCOUNT_NOT_FOUND)
            if (
                account.merchant_id != connection.merchant_id
                or account.environment != connection.environment
            ):
                raise OnboardingRejected(SCOPE_MISMATCH)
            if account.status != ReceivingAccountStatus.ACTIVE:
                raise OnboardingRejected(ACCOUNT_NOT_ACTIVE)
            if receiving_account_id not in await uow.connection_bindings.account_ids(connection.id):
                await uow.connection_bindings.add(
                    tenant_id=tenant_id,
                    merchant_id=connection.merchant_id,
                    environment=connection.environment,
                    connection_id=connection.id,
                    receiving_account_id=receiving_account_id,
                    created_by=actor,
                )
                await invalidate_readiness(
                    uow, connection, actor=actor, reason="account bound", now=self._clock.now()
                )
            updated = await uow.connections.get(tenant_id, connection_id)
            await uow.commit()
        logger.info(
            "payment_connection_account_bound",
            extra={
                "connection_id": str(connection_id),
                "account_id": str(receiving_account_id),
                "actor": actor,
            },
        )
        assert updated is not None
        return updated


class SetConnectionStatus:
    def __init__(self, uow_factory: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    async def execute(
        self,
        tenant_id: str,
        connection_id: UUID,
        status: ConnectionStatus,
        actor: str,
        reason: str,
    ) -> ProviderConnection:
        """Audited status change; ``reason`` is required.

        ``active`` needs an active bound account of the connection's merchant and
        environment (``NO_BOUND_ACCOUNT`` otherwise) and ``ready`` readiness for the active
        profile in that environment (``CONNECTION_NOT_READY``). ``not_ready`` is how an operator
        lets a profile activation go ahead without this connection.
        """
        require_actor(actor)
        _require_reason(reason)
        target = ConnectionStatus(status)
        async with self._uow_factory() as uow:
            active_profile = None
            if target == ConnectionStatus.ACTIVE:
                profiles = await uow.reference_profiles.share_all()
                active_profile = next(
                    (p for p in profiles if p.status == ProfileStatus.ACTIVE), None
                )
            connection = await _connection_for_update(uow, tenant_id, connection_id)
            if target not in _CONNECTION_TRANSITIONS[connection.status]:
                raise IllegalTransition(
                    "provider_connection", connection.status.value, target.value
                )
            if target == ConnectionStatus.ACTIVE:
                if active_profile is None:
                    raise OnboardingRejected(NO_ACTIVE_PROFILE)
                if not await has_active_binding(uow, connection):
                    raise OnboardingRejected(NO_BOUND_ACCOUNT)
                if not await is_ready(uow, connection, active_profile.version):
                    raise OnboardingRejected(CONNECTION_NOT_READY)
            await uow.connections.set_status(
                tenant_id,
                connection_id,
                target,
                changed_by=actor,
                changed_at=self._clock.now(),
                reason=reason,
            )
            updated = await uow.connections.get(tenant_id, connection_id)
            await uow.commit()
        logger.info(
            "payment_connection_status_changed",
            extra={"connection_id": str(connection_id), "status": target.value, "actor": actor},
        )
        assert updated is not None
        return updated


class SetReconcileMode:
    def __init__(self, uow_factory: UnitOfWorkFactory, evidence_verifier: EvidenceVerifier) -> None:
        self._uow_factory = uow_factory
        self._verifier = evidence_verifier

    async def execute(
        self,
        tenant_id: str,
        connection_id: UUID,
        mode: ReconcileMode,
        evidence_ref: str | None,
        actor: str,
    ) -> ProviderConnection:
        """``auto_settle`` needs evidence covering the connection's environment and every
        bound account; the verifier fails closed. ``detect_only`` clears the evidence."""
        require_actor(actor)
        target = ReconcileMode(mode)
        async with self._uow_factory() as uow:
            connection = await _connection_for_update(uow, tenant_id, connection_id)
            if target == ReconcileMode.AUTO_SETTLE:
                if not evidence_ref:
                    raise EvidenceRejected(NOT_FOUND, "an evidence reference is required")
                accounts = []
                for account_id in await uow.connection_bindings.account_ids(connection.id):
                    account = await uow.receiving_accounts.get(tenant_id, account_id)
                    if account is not None:
                        accounts.append(account)
                self._verifier.verify(evidence_ref, connection=connection, accounts=accounts)
            else:
                evidence_ref = None
            await uow.connections.set_reconcile_mode(tenant_id, connection.id, target, evidence_ref)
            updated = await uow.connections.get(tenant_id, connection_id)
            await uow.commit()
        logger.info(
            "payment_connection_reconcile_mode_changed",
            extra={
                "connection_id": str(connection_id),
                "mode": target.value,
                "evidence_ref": evidence_ref,
                "actor": actor,
            },
        )
        assert updated is not None
        return updated
