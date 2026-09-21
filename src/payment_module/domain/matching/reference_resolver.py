"""Second core step: find the one intent whose full reference appears as a token.

After a unique hit the resolver also checks scope. An intent of another tenant, another
environment or another receiving account (which covers another merchant of the same tenant)
is never handed to the policy.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from payment_module.domain.intent import IntentView
from payment_module.domain.reference import (
    NamedPrefix,
    PrefixOverlapReport,
    PrefixShape,
    check_prefix_overlap,
)
from payment_module.domain.transaction import TransactionView


class ResolutionKind(StrEnum):
    NONE = "NONE"
    UNIQUE = "UNIQUE"
    AMBIGUOUS = "AMBIGUOUS"


class ScopeMismatch(StrEnum):
    """Which part of the scope differs; stored in review ``details['scope']``."""

    TENANT = "tenant"
    ENVIRONMENT = "environment"
    RECEIVING_ACCOUNT = "receiving_account"


@dataclass(frozen=True, slots=True)
class Resolution:
    kind: ResolutionKind
    intent: IntentView | None = None


class ReferenceResolver:
    def resolve(
        self, tokens: Sequence[str], candidates_by_reference: Mapping[str, IntentView]
    ) -> Resolution:
        """Resolve normalized tokens against candidate intents keyed by full reference.

        ``code`` and ``content`` tokens pointing at two different intents are ambiguous.
        """
        found: dict[UUID, IntentView] = {}
        for token in tokens:
            intent = candidates_by_reference.get(token)
            if intent is None:
                continue
            if intent.payment_reference != token:
                raise ValueError("candidate intents must be keyed by their payment reference")
            found.setdefault(intent.id, intent)
        if not found:
            return Resolution(ResolutionKind.NONE)
        if len(found) > 1:
            return Resolution(ResolutionKind.AMBIGUOUS)
        return Resolution(ResolutionKind.UNIQUE, next(iter(found.values())))

    def scope_mismatch(self, tx: TransactionView, intent: IntentView) -> ScopeMismatch | None:
        if intent.tenant_id != tx.tenant_id:
            return ScopeMismatch.TENANT
        if intent.environment is not tx.environment:
            return ScopeMismatch.ENVIRONMENT
        if (
            intent.receiving_account_id != tx.receiving_account_id
            or intent.merchant_id != tx.merchant_id
        ):
            return ScopeMismatch.RECEIVING_ACCOUNT
        return None

    @staticmethod
    def check_prefix_overlap(
        candidate: Sequence[NamedPrefix], accepted: Sequence[PrefixShape]
    ) -> PrefixOverlapReport:
        """Equal or nested prefixes: errors within ``candidate``, advisories across versions."""
        return check_prefix_overlap(candidate, accepted)
