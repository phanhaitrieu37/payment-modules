from __future__ import annotations

import pytest

from payment_module.domain.enums import ProfileKind, ProfileStatus
from payment_module.domain.errors import (
    InvalidReferenceProfile,
    PrefixOverlap,
    UnknownReferencePrefix,
)
from payment_module.domain.matching.reference_resolver import ReferenceResolver
from payment_module.domain.reference import (
    NamedPrefix,
    PrefixShape,
    ReferenceProfile,
    check_prefix_overlap,
)
from payment_module.reference.random_suffix_generator import RandomSuffixGenerator

DIGITS = "0123456789"
LETTERS_AND_DIGITS = "ACDEFHJKMNPRTVWXY347"


def generated(
    *prefixes: NamedPrefix,
    version: int = 1,
    suffix_length: int = 8,
    alphabet: str = DIGITS,
    status: ProfileStatus = ProfileStatus.ACTIVE,
) -> ReferenceProfile:
    return ReferenceProfile(
        version=version,
        prefixes=prefixes,
        suffix_length=suffix_length,
        alphabet=alphabet,
        kind=ProfileKind.GENERATED,
        status=status,
    )


SUB = NamedPrefix("subscription", "SUB")
TOP = NamedPrefix("topup", "TOP")
ORD = NamedPrefix("order", "ORD")


@pytest.mark.parametrize(
    ("prefix", "valid"),
    [
        ("S", False),
        ("SU", True),
        ("SUBSC", True),
        ("SUBSCR", False),
        ("sub", False),
        ("SU1", False),
        ("SÜB", False),
        ("", False),
        ("SUB\n", False),
    ],
)
def test_prefix_value_is_two_to_five_ascii_capitals(prefix: str, valid: bool) -> None:
    if valid:
        assert NamedPrefix("subscription", prefix).prefix == prefix
    else:
        with pytest.raises(InvalidReferenceProfile):
            NamedPrefix("subscription", prefix)


@pytest.mark.parametrize(
    ("name", "valid"),
    [
        ("subscription", True),
        ("top_up2", True),
        ("a", True),
        ("a" * 32, True),
        ("a" * 33, False),
        ("Topup", False),
        ("2fa", False),
        ("_x", False),
        ("top-up", False),
    ],
)
def test_prefix_name_rules(name: str, valid: bool) -> None:
    if valid:
        assert NamedPrefix(name, "SUB").name == name
    else:
        with pytest.raises(InvalidReferenceProfile):
            NamedPrefix(name, "SUB")


@pytest.mark.parametrize(
    ("suffix_length", "valid"), [(0, False), (1, True), (30, True), (31, False)]
)
def test_suffix_length_bounds(suffix_length: int, valid: bool) -> None:
    if valid:
        assert generated(SUB, suffix_length=suffix_length).suffix_length == suffix_length
    else:
        with pytest.raises(InvalidReferenceProfile):
            generated(SUB, suffix_length=suffix_length)


@pytest.mark.parametrize(
    ("alphabet", "valid"),
    [
        (DIGITS, True),
        (LETTERS_AND_DIGITS, True),
        ("", False),
        ("abc123", False),
        ("AB-12", False),
        ("AAB", False),
    ],
)
def test_alphabet_is_shared_subset_of_capitals_and_digits(alphabet: str, valid: bool) -> None:
    if valid:
        assert generated(SUB, TOP, alphabet=alphabet).alphabet == alphabet
    else:
        with pytest.raises(InvalidReferenceProfile):
            generated(SUB, TOP, alphabet=alphabet)


def test_generated_profile_needs_a_prefix() -> None:
    with pytest.raises(InvalidReferenceProfile):
        generated()


def test_three_named_prefixes_share_suffix_and_alphabet() -> None:
    profile = generated(SUB, TOP, ORD, suffix_length=20, alphabet=LETTERS_AND_DIGITS)
    assert [shape.prefix for shape in profile.shapes()] == ["SUB", "TOP", "ORD"]
    assert {(s.suffix_length, s.alphabet, s.version) for s in profile.shapes()} == {
        (20, LETTERS_AND_DIGITS, 1)
    }


def test_duplicate_prefix_name_rejected() -> None:
    with pytest.raises(InvalidReferenceProfile):
        generated(SUB, NamedPrefix("subscription", "TOP"))


def test_duplicate_prefix_value_rejected() -> None:
    with pytest.raises(PrefixOverlap):
        generated(SUB, NamedPrefix("renewal", "SUB"))


def test_nested_prefix_rejected() -> None:
    with pytest.raises(PrefixOverlap) as caught:
        generated(SUB, NamedPrefix("subscription_x", "SUBX"))
    assert {caught.value.first.prefix, caught.value.second.prefix} == {"SUB", "SUBX"}


def test_prefix_for_returns_named_prefix() -> None:
    profile = generated(SUB, TOP)
    assert profile.prefix_for("topup") == "TOP"


def test_unknown_prefix_name_raises() -> None:
    with pytest.raises(UnknownReferencePrefix):
        generated(SUB).prefix_for("topup")


def test_same_prefix_across_versions_allowed_with_advisory() -> None:
    v1 = generated(SUB, TOP, version=1, suffix_length=24, status=ProfileStatus.ACTIVE)
    v2 = generated(SUB, TOP, version=2, suffix_length=20, status=ProfileStatus.DRAFT)

    report = ReferenceResolver.check_prefix_overlap(v2.prefixes, v1.shapes())

    assert report.errors == []
    assert {(a.candidate.prefix, a.accepted.suffix_length) for a in report.advisories} == {
        ("SUB", 24),
        ("TOP", 24),
    }


def test_nested_prefix_across_versions_is_advisory_only() -> None:
    accepted = (PrefixShape("SUBX", 10, DIGITS, 1),)
    report = check_prefix_overlap((SUB,), accepted)
    assert report.errors == []
    assert [a.accepted.prefix for a in report.advisories] == ["SUBX"]


def test_unrelated_prefixes_across_versions_report_nothing() -> None:
    report = check_prefix_overlap((SUB,), (PrefixShape("TOP", 8, DIGITS, 1),))
    assert report.errors == [] and report.advisories == []


def test_overlap_errors_within_candidate() -> None:
    report = check_prefix_overlap((SUB, TOP, NamedPrefix("sub_extra", "SUBX")), ())
    assert [(e.first.prefix, e.second.prefix) for e in report.errors] == [("SUB", "SUBX")]


def test_legacy_import_has_no_prefix_and_only_accepted_legacy() -> None:
    legacy = ReferenceProfile(
        version=1,
        prefixes=(),
        suffix_length=None,
        alphabet=None,
        kind=ProfileKind.LEGACY_IMPORT,
        status=ProfileStatus.ACCEPTED_LEGACY,
    )
    assert legacy.shapes() == ()
    with pytest.raises(UnknownReferencePrefix):
        legacy.prefix_for("subscription")


@pytest.mark.parametrize(
    ("prefixes", "status"),
    [
        ((SUB,), ProfileStatus.ACCEPTED_LEGACY),
        ((), ProfileStatus.ACTIVE),
        ((), ProfileStatus.DRAFT),
    ],
)
def test_legacy_import_rejects_prefixes_and_generating_statuses(
    prefixes: tuple[NamedPrefix, ...], status: ProfileStatus
) -> None:
    with pytest.raises(InvalidReferenceProfile):
        ReferenceProfile(1, prefixes, None, None, ProfileKind.LEGACY_IMPORT, status)


def test_version_must_be_positive_int() -> None:
    with pytest.raises(InvalidReferenceProfile):
        generated(SUB, version=0)


def test_generator_uses_named_prefix_and_profile_suffix() -> None:
    profile = generated(SUB, TOP, suffix_length=20, alphabet=LETTERS_AND_DIGITS)
    reference = RandomSuffixGenerator().generate(profile, "topup").value
    assert reference.startswith("TOP")
    assert len(reference) == 23
    assert set(reference[3:]) <= set(LETTERS_AND_DIGITS)


def test_generator_rejects_unknown_prefix_name() -> None:
    with pytest.raises(UnknownReferencePrefix):
        RandomSuffixGenerator().generate(generated(SUB), "unknown_prefix_name")
