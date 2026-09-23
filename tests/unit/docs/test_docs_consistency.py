"""The getting-started guide and the readmes stay consistent across languages and link only to
files that exist. The guide's code itself runs in
``tests/integration/docs/test_getting_started.py``."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCS = {
    "guide": (
        REPO_ROOT / "docs" / "getting-started.md",
        REPO_ROOT / "docs" / "getting-started.vi.md",
    ),
    "readme": (REPO_ROOT / "readme.md", REPO_ROOT / "readme.vi.md"),
}
FENCE = re.compile(r"^```[^\n]*\n.*?^```$", re.MULTILINE | re.DOTALL)
LINK = re.compile(r"\]\(([^)\s]+)\)")
HEADING = re.compile(r"^#{1,6} (.+)$", re.MULTILINE)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation dropped, spaces to hyphens."""
    kept = "".join(char for char in heading.strip().lower() if char.isalnum() or char in " -_")
    return kept.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    return {_slug(heading) for heading in HEADING.findall(FENCE.sub("", _text(path)))}


def test_the_vietnamese_guide_has_the_same_code_blocks() -> None:
    english, vietnamese = DOCS["guide"]
    assert FENCE.findall(_text(vietnamese)) == FENCE.findall(_text(english))


@pytest.mark.parametrize("pair", DOCS.values(), ids=DOCS.keys())
def test_each_language_links_to_the_other(pair: tuple[Path, Path]) -> None:
    english, vietnamese = pair
    assert f"]({vietnamese.name})" in _text(english)
    assert f"]({english.name})" in _text(vietnamese)


@pytest.mark.parametrize(
    "document", [path for pair in DOCS.values() for path in pair], ids=lambda path: path.name
)
def test_every_relative_link_resolves(document: Path) -> None:
    broken = []
    for target in LINK.findall(FENCE.sub("", _text(document))):
        if re.match(r"[a-z][a-z0-9+.-]*:", target):
            continue  # an absolute URL
        path_part, _, anchor = target.partition("#")
        linked = (document.parent / unquote(path_part)).resolve() if path_part else document
        missing_anchor = bool(anchor) and linked.suffix == ".md"
        if not linked.exists() or (missing_anchor and unquote(anchor) not in _anchors(linked)):
            broken.append(target)
    assert not broken, f"{document.name}: {broken}"
