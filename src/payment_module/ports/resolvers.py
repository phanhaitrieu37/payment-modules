"""Resolve a webhook locator to its connection, and a connection to its secrets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from payment_module.domain.enums import ConnectionStatus, Environment, ReconcileMode


@dataclass(frozen=True, slots=True)
class ProviderConnection:
    """One provider webhook of one merchant in one environment.

    ``secret_ref`` and ``api_credential_ref`` are references for :class:`SecretResolver`,
    never secret values.
    """

    id: UUID
    tenant_id: str
    merchant_id: UUID
    environment: Environment
    provider: str
    locator: str
    status: ConnectionStatus
    reconcile_mode: ReconcileMode
    timestamp_tolerance_seconds: int
    secret_ref: str
    api_credential_ref: str | None


class ConnectionResolver(Protocol):
    async def by_locator(self, locator: str) -> ProviderConnection | None:
        """Return the connection for a locator, or ``None`` when it does not exist."""
        ...


class SecretResolver(Protocol):
    async def webhook_secrets(self, connection: ProviderConnection) -> list[str]:
        """Every secret currently valid for signature checks (rotation window)."""
        ...

    async def api_credential(self, connection: ProviderConnection) -> str | None:
        """The company-level API credential, or ``None`` when reconciliation is not set up."""
        ...
