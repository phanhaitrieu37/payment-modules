"""Payment references, named prefixes and the versioned reference profile.

A prefix only helps recognition and webhook filtering; it is not authentication. Matching
always compares whole normalized tokens with the full stored reference, never
``startswith(prefix)``.

Normalization contract (version 1): split ``code`` and ``content`` on runs of characters
outside ``[A-Za-z0-9_-]`` and upper-case each token. No other character is removed, so a
reference glued to other text, or wrapped in ``-``/``_``, stays part of a longer token and
does not match.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from payment_module.domain.enums import ProfileKind, ProfileStatus, coerce_enum_fields
from payment_module.domain.errors import (
    InvalidPaymentReference,
    InvalidReferenceProfile,
    PrefixOverlap,
    UnknownReferencePrefix,
)

_PREFIX_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
_PREFIX_VALUE = re.compile(r"[A-Z]{2,5}")
_TOKEN_SEPARATORS = re.compile(r"[^A-Za-z0-9_-]+")
_TOKEN = re.compile(r"[A-Z0-9_-]+")
_ALPHABET_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
_MIN_SUFFIX_LENGTH = 1
_MAX_SUFFIX_LENGTH = 30
_LEGACY_STATUSES = frozenset({ProfileStatus.ACCEPTED_LEGACY, ProfileStatus.RETIRED})


@dataclass(frozen=True, slots=True)
class NamedPrefix:
    """A project-level prefix chosen by name, e.g. ``subscription -> SUB``."""

    name: str
    prefix: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _PREFIX_NAME.fullmatch(self.name):
            raise InvalidReferenceProfile(
                f"prefix name {self.name!r} must match [a-z][a-z0-9_]{{0,31}}"
            )
        if not isinstance(self.prefix, str) or not _PREFIX_VALUE.fullmatch(self.prefix):
            raise InvalidReferenceProfile(
                f"prefix {self.prefix!r} must be 2 to 5 upper-case letters A-Z"
            )


@dataclass(frozen=True, slots=True)
class PrefixShape:
    """The code shape one accepted profile version produces for one prefix."""

    prefix: str
    suffix_length: int
    alphabet: str
    version: int


@dataclass(frozen=True, slots=True)
class CrossVersionOverlap:
    """A candidate prefix equal to, or nested with, a prefix of another accepted version.

    Advisory only: references are unique per project and matched as whole tokens, so one
    string still belongs to one intent. The provider checklist uses it to widen the
    recognized suffix range.
    """

    candidate: NamedPrefix
    accepted: PrefixShape


@dataclass(frozen=True, slots=True)
class PrefixOverlapReport:
    errors: list[PrefixOverlap] = field(default_factory=list)
    advisories: list[CrossVersionOverlap] = field(default_factory=list)


def _nested(a: str, b: str) -> bool:
    return a.startswith(b) or b.startswith(a)


def check_prefix_overlap(
    candidate: Sequence[NamedPrefix], accepted: Sequence[PrefixShape]
) -> PrefixOverlapReport:
    """Report equal or nested prefixes.

    Pairs inside ``candidate`` (one profile version) are errors. Pairs between a candidate
    prefix and a prefix of another accepted version are advisories.
    """
    errors = [
        PrefixOverlap(first, second)
        for index, first in enumerate(candidate)
        for second in candidate[index + 1 :]
        if _nested(first.prefix, second.prefix)
    ]
    advisories = [
        CrossVersionOverlap(candidate=named, accepted=shape)
        for named in candidate
        for shape in accepted
        if _nested(named.prefix, shape.prefix)
    ]
    return PrefixOverlapReport(errors=errors, advisories=advisories)


@dataclass(frozen=True, slots=True)
class ReferenceProfile:
    """An immutable, versioned recipe for generating payment references.

    A ``generated`` version has one or more named prefixes that share ``suffix_length`` and
    ``alphabet``. A ``legacy_import`` version carries no prefixes, never generates codes and
    is only ever accepted for old host codes.
    """

    version: int
    prefixes: tuple[NamedPrefix, ...]
    suffix_length: int | None
    alphabet: str | None
    kind: ProfileKind
    status: ProfileStatus

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise InvalidReferenceProfile("version must be an int")
        if self.version < 1:
            raise InvalidReferenceProfile("version must be at least 1")
        coerce_enum_fields(self, kind=ProfileKind, status=ProfileStatus)
        if not isinstance(self.prefixes, tuple):
            raise InvalidReferenceProfile("prefixes must be a tuple")
        if self.kind == ProfileKind.LEGACY_IMPORT:
            self._validate_legacy()
        else:
            self._validate_generated()

    def _validate_legacy(self) -> None:
        if self.prefixes or self.suffix_length is not None or self.alphabet is not None:
            raise InvalidReferenceProfile(
                "a legacy_import profile has no prefixes, suffix length or alphabet"
            )
        if self.status not in _LEGACY_STATUSES:
            raise InvalidReferenceProfile(
                "a legacy_import profile can only be accepted_legacy or retired"
            )

    def _validate_generated(self) -> None:
        if not self.prefixes:
            raise InvalidReferenceProfile("a generated profile needs at least one prefix")
        names = [named.name for named in self.prefixes]
        if len(set(names)) != len(names):
            raise InvalidReferenceProfile("prefix names must be unique within a version")
        report = check_prefix_overlap(self.prefixes, ())
        if report.errors:
            raise report.errors[0]
        length = self.suffix_length
        if isinstance(length, bool) or not isinstance(length, int):
            raise InvalidReferenceProfile("suffix_length must be an int")
        if not _MIN_SUFFIX_LENGTH <= length <= _MAX_SUFFIX_LENGTH:
            raise InvalidReferenceProfile(
                f"suffix_length must be between {_MIN_SUFFIX_LENGTH} and {_MAX_SUFFIX_LENGTH}"
            )
        alphabet = self.alphabet
        if not isinstance(alphabet, str) or not alphabet:
            raise InvalidReferenceProfile("alphabet must be a non-empty string")
        if not set(alphabet) <= _ALPHABET_CHARS:
            raise InvalidReferenceProfile("alphabet may only use A-Z and 0-9")
        if len(set(alphabet)) != len(alphabet):
            raise InvalidReferenceProfile("alphabet must not repeat characters")

    def prefix_for(self, name: str) -> str:
        for named in self.prefixes:
            if named.name == name:
                return named.prefix
        raise UnknownReferencePrefix(name, self.version)

    def shapes(self) -> tuple[PrefixShape, ...]:
        """Code shapes of this version, for cross-version overlap checks."""
        if self.suffix_length is None or self.alphabet is None:
            return ()
        return tuple(
            PrefixShape(named.prefix, self.suffix_length, self.alphabet, self.version)
            for named in self.prefixes
        )


@dataclass(frozen=True, slots=True)
class PaymentReference:
    """A full payment reference: one upper-case token, stored with the intent."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not _TOKEN.fullmatch(self.value):
            raise InvalidPaymentReference(
                "payment reference must be one upper-case token of A-Z, 0-9, '_' or '-'"
            )

    @classmethod
    def normalize(cls, raw: str) -> PaymentReference:
        """Strip surrounding whitespace and upper-case, then validate."""
        return cls(raw.strip().upper())

    def __str__(self) -> str:
        return self.value


def tokens_from(code: str | None, content: str | None) -> list[str]:
    """Normalized candidate tokens from the provider ``code`` then ``content``.

    Duplicates are dropped; first-seen order is kept.
    """
    tokens: dict[str, None] = {}
    for text in (code, content):
        if not text:
            continue
        for piece in _TOKEN_SEPARATORS.split(text):
            if piece:
                tokens.setdefault(piece.upper(), None)
    return list(tokens)
