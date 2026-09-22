"""Create a payment intent for a host order and return the transfer instruction.

Every check runs before the insert, so a rejection never leaves a database error inside a
host transaction that joined this unit of work. A retry with an idempotency key already used
is answered first: the same request gets its intent back even if the intent has expired or
the account is no longer ready since; another request gets :class:`IdempotencyConflict`.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from payment_module.application import provider_for
from payment_module.domain.enums import (
    ConnectionStatus,
    MerchantStatus,
    ProfileKind,
    ProfileStatus,
    ReceivingAccountStatus,
)
from payment_module.domain.errors import (
    IdempotencyConflict,
    IntentRejected,
    InvalidAmount,
    InvalidPaymentReference,
    ReferenceSpaceExhausted,
    UnknownReferencePrefix,
)
from payment_module.domain.intent import IntentView
from payment_module.domain.money import AmountVnd
from payment_module.domain.reference import PaymentReference, ReferenceProfile
from payment_module.ports.clock import Clock
from payment_module.ports.provider import (
    PaymentProvider,
    ReceivingAccountView,
    TransferInstruction,
)
from payment_module.ports.reference_generator import PaymentReferenceGenerator
from payment_module.ports.resolvers import ProviderConnection
from payment_module.ports.unit_of_work import NewPaymentIntent, UnitOfWork, UnitOfWorkFactory

logger = logging.getLogger(__name__)

_REFERENCE_MAX_LENGTH = 64

type _Generate = Callable[[], str]

AMOUNT_NOT_POSITIVE = "AMOUNT_NOT_POSITIVE"
EXPIRES_AT_NOT_AWARE = "EXPIRES_AT_NOT_AWARE"
EXPIRES_IN_PAST = "EXPIRES_IN_PAST"
MERCHANT_INACTIVE = "MERCHANT_INACTIVE"
RECEIVING_ACCOUNT_NOT_READY = "RECEIVING_ACCOUNT_NOT_READY"
UNKNOWN_REFERENCE_PREFIX = "UNKNOWN_REFERENCE_PREFIX"
PROFILE_VERSION_NOT_ALLOWED = "PROFILE_VERSION_NOT_ALLOWED"
REFERENCE_OVERRIDE_WITH_PREFIX = "REFERENCE_OVERRIDE_WITH_PREFIX"
LEGACY_PROFILE_REQUIRED = "LEGACY_PROFILE_REQUIRED"
INVALID_REFERENCE = "INVALID_REFERENCE"
REFERENCE_ALREADY_USED = "REFERENCE_ALREADY_USED"


@dataclass(frozen=True, slots=True)
class CreateIntentCommand:
    """``prefix_name`` picks a named prefix of the active profile.

    ``reference_override`` only imports an old host code: it needs
    ``reference_profile_version`` of an ``accepted_legacy`` ``legacy_import`` profile and no
    ``prefix_name``. The normal path never takes a profile version from the host.
    """

    tenant_id: str
    merchant_id: UUID
    receiving_account_id: UUID
    amount_vnd: int
    host_ref_type: str
    host_ref_id: str
    idempotency_key: str
    expires_at: datetime
    prefix_name: str | None = None
    reference_override: str | None = None
    reference_profile_version: int | None = None


@dataclass(frozen=True, slots=True)
class CreateIntentResult:
    intent: IntentView
    instruction: TransferInstruction
    created: bool

    @property
    def payment_reference(self) -> str:
        return self.intent.payment_reference


def request_fingerprint(cmd: CreateIntentCommand) -> str:
    """sha256 of the canonical JSON of every field that defines the request."""
    canonical = {
        "merchant_id": str(cmd.merchant_id),
        "receiving_account_id": str(cmd.receiving_account_id),
        "amount_vnd": cmd.amount_vnd,
        "host_ref_type": cmd.host_ref_type,
        "host_ref_id": cmd.host_ref_id,
        "expires_at": cmd.expires_at.astimezone(UTC).isoformat(),
        "prefix_name": cmd.prefix_name,
        "reference_override": cmd.reference_override,
        "reference_profile_version": cmd.reference_profile_version,
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class CreateIntent:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        providers: Mapping[str, PaymentProvider],
        reference_generator: PaymentReferenceGenerator,
        clock: Clock,
    ) -> None:
        self._uow_factory = uow_factory
        self._providers = providers
        self._generator = reference_generator
        self._clock = clock

    async def execute(
        self, cmd: CreateIntentCommand, uow: UnitOfWork | None = None
    ) -> CreateIntentResult:
        """Create the intent, or return the one this idempotency key already created.

        Pass ``uow`` (a joined unit of work the host has not entered yet) to create the
        intent in the host's transaction; otherwise the use case commits its own.
        """
        amount = self._validate(cmd)
        work = uow if uow is not None else self._uow_factory()
        async with work:
            result = await self._create(work, cmd, amount)
            await work.commit()
        logger.info(
            "payment_intent_created" if result.created else "payment_intent_replayed",
            extra={"intent_id": str(result.intent.id), "tenant_id": cmd.tenant_id},
        )
        return result

    def _validate(self, cmd: CreateIntentCommand) -> AmountVnd:
        try:
            amount = AmountVnd(cmd.amount_vnd)
        except InvalidAmount as exc:
            raise IntentRejected(AMOUNT_NOT_POSITIVE) from exc
        if amount.value <= 0:
            raise IntentRejected(AMOUNT_NOT_POSITIVE)
        expires_at = cmd.expires_at
        if expires_at.tzinfo is None or expires_at.utcoffset() is None:
            raise IntentRejected(EXPIRES_AT_NOT_AWARE)
        if cmd.reference_override is not None and cmd.prefix_name is not None:
            raise IntentRejected(REFERENCE_OVERRIDE_WITH_PREFIX)
        if cmd.reference_override is None and cmd.reference_profile_version is not None:
            raise IntentRejected(PROFILE_VERSION_NOT_ALLOWED)
        return amount

    async def _create(
        self, uow: UnitOfWork, cmd: CreateIntentCommand, amount: AmountVnd
    ) -> CreateIntentResult:
        fingerprint = request_fingerprint(cmd)
        replay = await self._replay(uow, cmd, fingerprint)
        if replay is not None:
            return replay
        if cmd.expires_at <= self._clock.now():
            raise IntentRejected(EXPIRES_IN_PAST)
        account, connection = await self._receiving_scope(uow, cmd)
        provider = provider_for(self._providers, connection.provider)
        profile, prefix_name, generate = await self._reference(uow, cmd)
        new = NewPaymentIntent(
            tenant_id=cmd.tenant_id,
            merchant_id=cmd.merchant_id,
            environment=account.environment,
            receiving_account_id=account.id,
            amount=amount,
            beneficiary_snapshot={
                "bank_code": account.bank_code,
                "account_number": account.account_number,
                "sub_account": account.sub_account,
                "account_name": account.account_name,
            },
            reference_profile_version=profile.version,
            reference_prefix_name=prefix_name,
            host_ref_type=cmd.host_ref_type,
            host_ref_id=cmd.host_ref_id,
            idempotency_key=cmd.idempotency_key,
            request_fingerprint=fingerprint,
            expires_at=cmd.expires_at,
        )
        if cmd.reference_override is None:
            intent, created = await uow.intents.create(new, generate)
        else:
            # One attempt: a fixed legacy code that collides is taken, not unlucky.
            try:
                intent, created = await uow.intents.create(new, generate, max_attempts=1)
            except ReferenceSpaceExhausted as exc:
                raise IntentRejected(REFERENCE_ALREADY_USED) from exc
        return CreateIntentResult(intent, provider.build_instruction(intent, account), created)

    async def _replay(
        self, uow: UnitOfWork, cmd: CreateIntentCommand, fingerprint: str
    ) -> CreateIntentResult | None:
        """The earlier answer for this idempotency key, before any readiness check."""
        account = await uow.receiving_accounts.get(cmd.tenant_id, cmd.receiving_account_id)
        if account is None or account.merchant_id != cmd.merchant_id:
            return None
        found = await uow.intents.find_by_idempotency_key(
            cmd.tenant_id, account.environment, cmd.idempotency_key
        )
        if found is None:
            return None
        intent, stored_fingerprint = found
        if stored_fingerprint != fingerprint:
            raise IdempotencyConflict(
                f"idempotency key {cmd.idempotency_key!r} was used for another request"
            )
        for connection_id in await uow.connection_bindings.connection_ids_for_account(account.id):
            connection = await uow.connections.get(cmd.tenant_id, connection_id)
            if connection is not None:
                provider = provider_for(self._providers, connection.provider)
                return CreateIntentResult(
                    intent, provider.build_instruction(intent, account), False
                )
        raise IntentRejected(RECEIVING_ACCOUNT_NOT_READY)

    async def _receiving_scope(
        self, uow: UnitOfWork, cmd: CreateIntentCommand
    ) -> tuple[ReceivingAccountView, ProviderConnection]:
        """Merchant and account active in the tenant, with an active connection bound to the
        account. The intent's environment is the account's, never a value from the host."""
        merchant = await uow.merchants.get(cmd.tenant_id, cmd.merchant_id)
        if merchant is None or merchant.status != MerchantStatus.ACTIVE:
            raise IntentRejected(MERCHANT_INACTIVE)
        account = await uow.receiving_accounts.get(cmd.tenant_id, cmd.receiving_account_id)
        if (
            account is None
            or account.merchant_id != cmd.merchant_id
            or account.status != ReceivingAccountStatus.ACTIVE
        ):
            raise IntentRejected(RECEIVING_ACCOUNT_NOT_READY)
        for connection_id in await uow.connection_bindings.connection_ids_for_account(account.id):
            connection = await uow.connections.get(cmd.tenant_id, connection_id)
            if (
                connection is not None
                and connection.status == ConnectionStatus.ACTIVE
                and connection.merchant_id == account.merchant_id
                and connection.environment == account.environment
            ):
                return account, connection
        raise IntentRejected(RECEIVING_ACCOUNT_NOT_READY)

    async def _reference(
        self, uow: UnitOfWork, cmd: CreateIntentCommand
    ) -> tuple[ReferenceProfile, str | None, _Generate]:
        if cmd.reference_override is not None:
            return await self._legacy_reference(uow, cmd)
        profile = await uow.reference_profiles.get_active()
        if profile is None:
            raise IntentRejected(RECEIVING_ACCOUNT_NOT_READY)
        if cmd.prefix_name is None:
            raise IntentRejected(UNKNOWN_REFERENCE_PREFIX)
        prefix_name = cmd.prefix_name
        try:
            profile.prefix_for(prefix_name)
        except UnknownReferencePrefix as exc:
            raise IntentRejected(UNKNOWN_REFERENCE_PREFIX) from exc

        def generate() -> str:
            return self._generator.generate(profile, prefix_name).value

        return profile, prefix_name, generate

    @staticmethod
    async def _legacy_reference(
        uow: UnitOfWork, cmd: CreateIntentCommand
    ) -> tuple[ReferenceProfile, None, _Generate]:
        assert cmd.reference_override is not None
        if cmd.reference_profile_version is None:
            raise IntentRejected(LEGACY_PROFILE_REQUIRED)
        profile = await uow.reference_profiles.get(cmd.reference_profile_version)
        if (
            profile is None
            or profile.kind != ProfileKind.LEGACY_IMPORT
            or profile.status != ProfileStatus.ACCEPTED_LEGACY
        ):
            raise IntentRejected(LEGACY_PROFILE_REQUIRED)
        try:
            reference = PaymentReference.normalize(cmd.reference_override).value
        except InvalidPaymentReference as exc:
            raise IntentRejected(INVALID_REFERENCE) from exc
        if len(reference) > _REFERENCE_MAX_LENGTH:
            raise IntentRejected(INVALID_REFERENCE)
        return profile, None, lambda: reference
