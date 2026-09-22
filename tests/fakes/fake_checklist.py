"""A provider template checklist for application tests, written against the port only.

It follows the provider checklist scheme: items are keyed by prefix **value**
(``template:SUB``, ``webhook_filter:SUB``), and a template item's suffix range covers the
candidate and every accepted version using the same prefix. Shared items are
``recognition:global``, ``template:order``, ``webhook:url``, ``webhook:hmac``,
``accounts:bound`` and ``environment``.
"""

from __future__ import annotations

from collections.abc import Sequence

from payment_module.domain.reference import PrefixShape, ReferenceProfile
from payment_module.ports.resolvers import ProviderConnection
from payment_module.ports.template_checklist import Checklist, ChecklistItem, Issue

SHARED_ITEMS = (
    "recognition:global",
    "template:order",
    "webhook:url",
    "webhook:hmac",
    "accounts:bound",
    "environment",
)


class FakeChecklist:
    def __init__(self, issues: Sequence[Issue] = ()) -> None:
        self.issues = list(issues)
        self.calls: list[tuple[int, tuple[PrefixShape, ...]]] = []

    def validate(self, profile: ReferenceProfile) -> list[Issue]:
        return list(self.issues)

    def checklist(
        self,
        connection: ProviderConnection,
        profile: ReferenceProfile,
        accepted: Sequence[PrefixShape] = (),
    ) -> Checklist:
        self.calls.append((profile.version, tuple(accepted)))
        shapes = [*profile.shapes(), *accepted]
        items = [ChecklistItem(key, key) for key in SHARED_ITEMS]
        for prefix in sorted({shape.prefix for shape in shapes}):
            same = [shape for shape in shapes if shape.prefix == prefix]
            lengths = [shape.suffix_length for shape in same]
            alphabet = "".join(sorted({char for shape in same for char in shape.alphabet}))
            items.append(
                ChecklistItem(
                    f"template:{prefix}",
                    f"code template for {prefix}",
                    {
                        "prefix": prefix,
                        "suffix_min": str(min(lengths)),
                        "suffix_max": str(max(lengths)),
                        "alphabet": alphabet,
                    },
                )
            )
            items.append(
                ChecklistItem(
                    f"webhook_filter:{prefix}", f"webhook filter for {prefix}", {"prefix": prefix}
                )
            )
        return Checklist(tuple(items))

    def item(self, profile: ReferenceProfile, key: str, accepted=()) -> ChecklistItem:
        """The item ``key`` of ``profile``'s checklist, for assertions."""
        found = self.checklist(None, profile, accepted).items  # type: ignore[arg-type]
        return next(item for item in found if item.key == key)


def all_confirmed(checklist: Checklist, *, except_keys: Sequence[str] = ()) -> dict[str, bool]:
    """Every item confirmed except ``except_keys``."""
    return {item.key: True for item in checklist.items if item.key not in except_keys}
