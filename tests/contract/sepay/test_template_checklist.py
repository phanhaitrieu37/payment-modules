"""SePay template rules and the per-prefix setup checklist."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

import pytest

from payment_module.adapters.sepay.checklist import SePayTemplateChecklist
from payment_module.domain.enums import (
    ConnectionStatus,
    Environment,
    ProfileKind,
    ProfileStatus,
    ReconcileMode,
)
from payment_module.domain.reference import NamedPrefix, PrefixShape, ReferenceProfile
from payment_module.ports.resolvers import ProviderConnection

pytestmark = pytest.mark.contract

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
COMMON = {
    "recognition:global",
    "template:order",
    "webhook:url",
    "webhook:hmac",
    "accounts:bound",
    "environment",
}


def connection() -> ProviderConnection:
    return ProviderConnection(
        id=uuid4(),
        tenant_id="t",
        merchant_id=uuid4(),
        environment=Environment.TEST,
        provider="sepay",
        locator="loc-1",
        status=ConnectionStatus.PENDING,
        reconcile_mode=ReconcileMode.DETECT_ONLY,
        timestamp_tolerance_seconds=300,
        secret_ref="ref",
        api_credential_ref=None,
    )


def profile(
    version: int, prefixes: dict[str, str], suffix: int = 6, alphabet: str = ALPHABET
) -> ReferenceProfile:
    return ReferenceProfile(
        version=version,
        prefixes=tuple(NamedPrefix(name, value) for name, value in prefixes.items()),
        suffix_length=suffix,
        alphabet=alphabet,
        kind=ProfileKind.GENERATED,
        status=ProfileStatus.DRAFT,
    )


@dataclass
class RawProfile:
    """Profile values the domain would refuse to build, to exercise provider validation."""

    prefixes: tuple[NamedPrefix, ...]
    suffix_length: int | None = 6
    alphabet: str | None = ALPHABET
    kind: ProfileKind = ProfileKind.GENERATED


def raw_prefix(name: str, value: str) -> NamedPrefix:
    named = object.__new__(NamedPrefix)
    object.__setattr__(named, "name", name)
    object.__setattr__(named, "prefix", value)
    return named


def keys(items) -> list[str]:
    return [item.key for item in items]


def test_three_named_prefixes_give_one_template_and_one_filter_each() -> None:
    checklist = SePayTemplateChecklist().checklist(
        connection(), profile(1, {"subscription": "SUB", "topup": "TOP", "invoice": "INV"})
    )
    found = keys(checklist.items)
    assert len(found) == len(set(found))
    templates = {k for k in found if k.startswith("template:") and k != "template:order"}
    filters = {k for k in found if k.startswith("webhook_filter:")}
    assert templates == {"template:SUB", "template:TOP", "template:INV"}
    assert filters == {"webhook_filter:SUB", "webhook_filter:TOP", "webhook_filter:INV"}
    assert set(found) - templates - filters == COMMON
    sub = next(item for item in checklist.items if item.key == "template:SUB")
    assert sub.params == {
        "prefix": "SUB",
        "suffix_min": "6",
        "suffix_max": "6",
        "alphabet": "".join(sorted(ALPHABET)),
        "charset": "alphanumeric",
    }


def test_suffix_range_covers_every_accepted_version_of_a_prefix() -> None:
    v1 = profile(1, {"subscription": "SUB", "topup": "TOP"}, suffix=24)
    v2 = profile(2, {"subscription": "SUB", "topup": "TOP"}, suffix=20)
    checklist = SePayTemplateChecklist().checklist(connection(), v2, accepted=v1.shapes())
    by_key = {item.key: item for item in checklist.items}
    for prefix in ("SUB", "TOP"):
        assert by_key[f"template:{prefix}"].params["suffix_min"] == "20"
        assert by_key[f"template:{prefix}"].params["suffix_max"] == "24"


def test_prefix_only_in_an_older_accepted_version_keeps_its_items() -> None:
    v1 = profile(1, {"legacy_plan": "OLD"}, suffix=8, alphabet="0123456789")
    v2 = profile(2, {"subscription": "SUB"})
    by_key = {
        item.key: item
        for item in SePayTemplateChecklist().checklist(connection(), v2, v1.shapes()).items
    }
    assert "webhook_filter:OLD" in by_key
    assert by_key["template:OLD"].params["suffix_min"] == "8"
    assert by_key["template:OLD"].params["charset"] == "numeric"


def test_nested_prefixes_across_versions_put_the_longer_prefix_first() -> None:
    old = [PrefixShape("SUB", 6, ALPHABET, 1)]
    candidate = profile(2, {"subscription": "SUBX"})
    checklist = SePayTemplateChecklist().checklist(connection(), candidate, old)
    order = next(item for item in checklist.items if item.key == "template:order")
    assert order.params["order"] == "SUBX,SUB"


def test_webhook_items_name_the_connection() -> None:
    conn = connection()
    by_key = {
        item.key: item
        for item in SePayTemplateChecklist().checklist(conn, profile(1, {"s": "SUB"})).items
    }
    assert by_key["webhook:url"].params == {"locator": "loc-1"}
    assert by_key["environment"].params == {"environment": "test"}


def test_valid_profile_has_no_issues() -> None:
    assert SePayTemplateChecklist().validate(profile(1, {"subscription": "SUB"})) == []


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (RawProfile((raw_prefix("long", "SUBSCR"),)), "prefix_shape"),
        (RawProfile((raw_prefix("short", "S"),)), "prefix_shape"),
        (RawProfile((raw_prefix("digit", "SU1"),)), "prefix_shape"),
        (RawProfile((raw_prefix("a", "SUB"), raw_prefix("b", "SUBX"))), "prefix_overlap"),
        (RawProfile((raw_prefix("a", "SUB"), raw_prefix("b", "SUB"))), "prefix_overlap"),
        (RawProfile((raw_prefix("a", "SUB"),), suffix_length=31), "suffix_length"),
        (RawProfile((raw_prefix("a", "SUB"),), suffix_length=0), "suffix_length"),
        (RawProfile((raw_prefix("a", "SUB"),), alphabet="abc"), "alphabet"),
    ],
)
def test_provider_limits_are_reported(raw: RawProfile, code: str) -> None:
    issues = SePayTemplateChecklist().validate(raw)  # type: ignore[arg-type]
    assert code in {issue.code for issue in issues}


def test_legacy_profile_has_nothing_to_validate() -> None:
    legacy = ReferenceProfile(1, (), None, None, ProfileKind.LEGACY_IMPORT, ProfileStatus.RETIRED)
    assert SePayTemplateChecklist().validate(legacy) == []
