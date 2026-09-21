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
