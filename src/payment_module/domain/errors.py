"""Domain exceptions.

Every exception raised by the pure domain derives from :class:`DomainError` so callers can
separate business rejections from infrastructure failures.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from payment_module.domain.reference import NamedPrefix


class DomainError(Exception):
    """Base class for rule violations detected by the domain."""


class InvalidAmount(DomainError, ValueError):
    """A value cannot be interpreted as a whole, non-negative VND amount."""


class InvalidAccountIdentity(DomainError, ValueError):
    """Bank code, account number or sub-account cannot form a stable account key."""


class InvalidReferenceProfile(DomainError, ValueError):
    """A reference profile or one of its named prefixes breaks the profile rules."""


class PrefixOverlap(InvalidReferenceProfile):
    """Two prefixes of the same profile version are equal or one starts with the other."""

    def __init__(self, first: NamedPrefix, second: NamedPrefix) -> None:
        self.first = first
        self.second = second
        super().__init__(
            f"prefix {first.prefix!r} ({first.name}) overlaps {second.prefix!r} ({second.name})"
        )


class UnknownReferencePrefix(DomainError, LookupError):
    """The profile has no prefix with the requested name."""

    def __init__(self, name: str, version: int) -> None:
        self.name = name
        self.version = version
        super().__init__(f"reference profile v{version} has no prefix named {name!r}")


class InvalidPaymentReference(DomainError, ValueError):
    """A payment reference is empty or is not a single normalized token."""


class IllegalTransition(DomainError):
    """A state change that the lifecycle does not allow."""

    def __init__(self, entity: str, current: str, target: str) -> None:
        self.entity = entity
        self.current = current
        self.target = target
        super().__init__(f"{entity}: illegal transition {current} -> {target}")


class PolicyViolation(DomainError):
    """A matching policy tried to widen what the core allows.

    This signals a bug in the host policy. The transaction is never settled because of it.
    """


class IdempotencyConflict(DomainError):
    """The idempotency key is already used by an intent created from a different request."""


class ReferenceSpaceExhausted(DomainError):
    """Every generated reference collided with an existing one; the profile is too small."""


class IntentRejected(DomainError):
    """``CreateIntent`` refused the request; ``code`` is a stable reason for the host."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


class IntentNotFound(DomainError, LookupError):
    """No intent with this id exists in the tenant."""


class ConnectionNotFound(DomainError, LookupError):
    """The webhook locator is unknown or its connection is disabled.

    Both cases carry the same message so a caller cannot tell them apart.
    """

    def __init__(self) -> None:
        super().__init__("webhook endpoint not found")


class WebhookAuthError(DomainError):
    """A delivery failed verification.

    ``code`` is one of ``missing_header``, ``invalid_signature`` or ``stale_timestamp``.
    """

    MISSING_HEADER = "missing_header"
    INVALID_SIGNATURE = "invalid_signature"
    STALE_TIMESTAMP = "stale_timestamp"

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"webhook verification failed: {code}")


class PayloadTooLarge(DomainError):
    """The delivery body exceeds the configured size limit."""
