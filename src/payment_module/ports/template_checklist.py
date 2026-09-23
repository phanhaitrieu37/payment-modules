"""Provider-side reference template rules and the manual setup checklist."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from payment_module.domain.reference import PrefixShape, ReferenceProfile
from payment_module.ports.resolvers import ProviderConnection


@dataclass(frozen=True, slots=True)
class Issue:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class ChecklistItem:
    """One manual step, keyed stably (for example ``template:SUB``)."""

    key: str
    description: str
    params: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Checklist:
    items: tuple[ChecklistItem, ...]


class ReferenceTemplateChecklist(Protocol):
    def validate(self, profile: ReferenceProfile) -> list[Issue]:
        """Provider limits the profile breaks; empty when the provider can recognize it."""
        ...

    def checklist(
        self,
        connection: ProviderConnection,
        profile: ReferenceProfile,
        accepted: Sequence[PrefixShape] = (),
    ) -> Checklist:
        """One template item and one webhook filter item per named prefix, plus shared items.

        ``accepted`` holds the shapes of other versions still accepted, so a template can
        cover every suffix length in use for the same prefix.
        """
        ...
