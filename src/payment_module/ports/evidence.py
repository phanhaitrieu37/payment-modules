"""Evidence gate for turning on ``auto_settle`` for a connection.

Reconciliation may settle from API sightings only when verification evidence proves the
provider's bank reference links webhook and API sightings for every account the connection
receives on. A verifier fails closed: anything it cannot prove is a rejection.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from payment_module.domain.enums import Environment, coerce_enum_fields
from payment_module.domain.errors import EvidenceRejected
from payment_module.ports.provider import ReceivingAccountView
from payment_module.ports.resolvers import ProviderConnection

NOT_FOUND = "not_found"
BAD_SCHEMA = "bad_schema"
BAD_DIGEST = "bad_digest"
STALE = "stale"
SCENARIO_A_NOT_PASS = "scenario_a_not_pass"
ACCOUNT_NOT_COVERED = "account_not_covered"
ENVIRONMENT_MISMATCH = "environment_mismatch"
PATH_OUTSIDE_ROOT = "path_outside_root"
VERIFIER_NOT_CONFIGURED = "verifier_not_configured"


@dataclass(frozen=True, slots=True)
class EvidenceReport:
    """What an accepted artifact proved; ``account_fingerprints`` are the covered accounts."""

    evidence_ref: str
    environment: Environment
    generated_at: datetime
    account_fingerprints: tuple[str, ...]

    def __post_init__(self) -> None:
        coerce_enum_fields(self, environment=Environment)


class EvidenceVerifier(Protocol):
    def verify(
        self,
        evidence_ref: str,
        *,
        connection: ProviderConnection,
        accounts: Sequence[ReceivingAccountView],
    ) -> EvidenceReport:
        """Accept ``evidence_ref`` only if it proves ``auto_settle`` for the connection's
        environment and every one of ``accounts``; otherwise raise
        :class:`EvidenceRejected` with one of this module's codes."""
        ...


class RejectAllEvidence:
    """The default when the host configured no verifier: ``auto_settle`` stays off."""

    def verify(
        self,
        evidence_ref: str,
        *,
        connection: ProviderConnection,
        accounts: Sequence[ReceivingAccountView],
    ) -> EvidenceReport:
        raise EvidenceRejected(VERIFIER_NOT_CONFIGURED, "no evidence verifier is configured")
