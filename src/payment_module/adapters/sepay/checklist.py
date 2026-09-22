"""SePay reference template rules and the manual setup checklist of one connection.

SePay limits (payment-code configuration docs, S17): prefix 2-5 characters, suffix length
1-30, suffix characters either digits or letters and digits. Recognition is a company-level
switch; templates are company-level and the first matching one wins; prefix filters are
per webhook. Nothing here provisions SePay: the checklist lists manual steps only.

Items are keyed by prefix **value** (``template:SUB``, ``webhook_filter:SUB``) so a key stays
stable when a prefix name changes between versions.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from payment_module.domain.enums import ProfileKind
from payment_module.domain.reference import PrefixShape, ReferenceProfile
from payment_module.ports.resolvers import ProviderConnection
from payment_module.ports.template_checklist import Checklist, ChecklistItem, Issue

_PREFIX = re.compile(r"[A-Z]{2,5}")
_DIGITS = frozenset("0123456789")
_ALPHANUMERIC = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
_MIN_SUFFIX, _MAX_SUFFIX = 1, 30


@dataclass(frozen=True, slots=True)
class _Template:
    prefix: str
    suffix_min: int
    suffix_max: int
    alphabet: frozenset[str]

    @property
    def charset(self) -> str:
        return "numeric" if self.alphabet <= _DIGITS else "alphanumeric"


def _nested(a: str, b: str) -> bool:
    return a.startswith(b) or b.startswith(a)


class SePayTemplateChecklist:
    def validate(self, profile: ReferenceProfile) -> list[Issue]:
        if profile.kind == ProfileKind.LEGACY_IMPORT:
            return []
        issues: list[Issue] = []
        for named in profile.prefixes:
            if not _PREFIX.fullmatch(named.prefix):
                issues.append(
                    Issue(
                        "prefix_shape",
                        f"prefix {named.prefix!r} ({named.name}) must be 2 to 5 letters A-Z",
                    )
                )
        for index, first in enumerate(profile.prefixes):
            for second in profile.prefixes[index + 1 :]:
                if _nested(first.prefix, second.prefix):
                    issues.append(
                        Issue(
                            "prefix_overlap",
                            f"prefix {first.prefix!r} overlaps {second.prefix!r}",
                        )
                    )
        length = profile.suffix_length
        if length is None or not _MIN_SUFFIX <= length <= _MAX_SUFFIX:
            issues.append(
                Issue("suffix_length", f"suffix length must be {_MIN_SUFFIX} to {_MAX_SUFFIX}")
            )
        if not profile.alphabet or not set(profile.alphabet) <= _ALPHANUMERIC:
            issues.append(Issue("alphabet", "suffix alphabet may only use A-Z and 0-9"))
        return issues

    def checklist(
        self,
        connection: ProviderConnection,
        profile: ReferenceProfile,
        accepted: Sequence[PrefixShape] = (),
    ) -> Checklist:
        templates = _templates(profile, accepted)
        items = [
            ChecklistItem(
                "recognition:global",
                "Turn on payment-code recognition for the SePay company",
            )
        ]
        items += [
            ChecklistItem(
                f"template:{template.prefix}",
                f"Add a recognition template for prefix {template.prefix}",
                {
                    "prefix": template.prefix,
                    "suffix_min": str(template.suffix_min),
                    "suffix_max": str(template.suffix_max),
                    "alphabet": "".join(sorted(template.alphabet)),
                    "charset": template.charset,
                },
            )
            for template in templates
        ]
        items.append(
            ChecklistItem(
                "template:order",
                "Order the templates so a longer nested prefix comes first",
                {"order": ",".join(template.prefix for template in templates)},
            )
        )
        items += [
            ChecklistItem(
                f"webhook_filter:{template.prefix}",
                f"Allow prefix {template.prefix} in this webhook's code filter",
                {"prefix": template.prefix, "locator": connection.locator},
            )
            for template in templates
        ]
        items += [
            ChecklistItem(
                "webhook:url",
                "Point the webhook at this connection's locator URL",
                {"locator": connection.locator},
            ),
            ChecklistItem("webhook:hmac", "Use HMAC-SHA256 authentication with the stored secret"),
            ChecklistItem(
                "accounts:bound",
                "Limit the webhook to the bank accounts bound to this connection",
            ),
            ChecklistItem(
                "environment",
                "Configure the SePay environment that matches the connection",
                {"environment": connection.environment.value},
            ),
        ]
        return Checklist(tuple(items))


def _templates(profile: ReferenceProfile, accepted: Sequence[PrefixShape]) -> list[_Template]:
    """One template per prefix value in the candidate or any other accepted version.

    Suffix min/max and the alphabet cover every version that uses the prefix, so money for a
    code of an older version is still recognized. Longer prefixes come first.
    """
    shapes = [*profile.shapes(), *accepted]
    by_prefix: dict[str, _Template] = {}
    for shape in shapes:
        current = by_prefix.get(shape.prefix)
        alphabet = frozenset(shape.alphabet)
        if current is None:
            by_prefix[shape.prefix] = _Template(
                shape.prefix, shape.suffix_length, shape.suffix_length, alphabet
            )
        else:
            by_prefix[shape.prefix] = _Template(
                shape.prefix,
                min(current.suffix_min, shape.suffix_length),
                max(current.suffix_max, shape.suffix_length),
                current.alphabet | alphabet,
            )
    return sorted(by_prefix.values(), key=lambda template: (-len(template.prefix), template.prefix))
