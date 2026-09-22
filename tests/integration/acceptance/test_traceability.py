"""The traceability map stays true: every case of the acceptance table and every invariant
has tests, and every mapped node id names a test function that exists."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from traceability import CASES, INVARIANTS, PLAN_CASES

pytestmark = [pytest.mark.integration, pytest.mark.acceptance]

REPO_ROOT = Path(__file__).resolve().parents[3]
CASE_ROW = re.compile(r"^\| (?P<case>[^|]+?) \| [^|]+ \|$")


def document_cases() -> list[str]:
    text = (REPO_ROOT / "validation-and-acceptance.md").read_text(encoding="utf-8")
    table = text.split("## Acceptance cases", 1)[1].split("\n\n", 2)[1]
    rows = [CASE_ROW.match(line) for line in table.splitlines()[2:]]
    return [row["case"] for row in rows if row is not None]


def test_cases_are_exactly_the_documented_fourteen() -> None:
    cases = document_cases()
    assert len(cases) == 14
    assert list(CASES) == cases


def test_every_invariant_and_case_has_tests() -> None:
    assert sorted(INVARIANTS) == list(range(1, 12))
    for name, node_ids in {**CASES, **PLAN_CASES, **INVARIANTS}.items():
        assert node_ids, f"{name} has no test"


def _functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def test_every_node_id_names_an_existing_test() -> None:
    node_ids = {
        node_id
        for group in (CASES, PLAN_CASES, INVARIANTS)
        for ids in group.values()
        for node_id in ids
    }
    missing = []
    for node_id in sorted(node_ids):
        path, function = node_id.split("::")
        file = REPO_ROOT / path
        if not file.is_file() or function.split("[")[0] not in _functions(file):
            missing.append(node_id)
    assert missing == []
