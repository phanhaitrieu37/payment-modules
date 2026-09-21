"""The domain and ports layers stay free of framework and infrastructure imports.

The domain also never imports the ports layer; ports may import the domain.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

import payment_module

PACKAGE_ROOT = Path(payment_module.__file__).parent
FORBIDDEN_EVERYWHERE = ("sqlalchemy", "fastapi", "httpx", "starlette", "asyncpg", "alembic")


def _modules(layer: str) -> list[Path]:
    return sorted((PACKAGE_ROOT / layer).rglob("*.py"))


def _imported_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def _violations(paths: list[Path], forbidden: tuple[str, ...]) -> list[str]:
    return [
        f"{path.relative_to(PACKAGE_ROOT)} imports {name}"
        for path in paths
        for name in sorted(_imported_names(path))
        if any(name == banned or name.startswith(f"{banned}.") for banned in forbidden)
    ]


@pytest.mark.parametrize("layer", ["domain", "ports"])
def test_layer_has_no_infrastructure_imports(layer: str) -> None:
    paths = _modules(layer)
    assert paths, f"no modules found under {layer}"
    assert _violations(paths, FORBIDDEN_EVERYWHERE) == []


def test_domain_does_not_import_ports() -> None:
    assert _violations(_modules("domain"), ("payment_module.ports",)) == []


def test_relative_imports_are_not_used() -> None:
    # Relative imports would bypass the checks above.
    relative = [
        str(path.relative_to(PACKAGE_ROOT))
        for layer in ("domain", "ports")
        for path in _modules(layer)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom) and node.level > 0
    ]
    assert relative == []


@pytest.mark.parametrize("layer", ["domain", "ports", "reference"])
def test_every_module_imports_cleanly(layer: str) -> None:
    for path in _modules(layer):
        module = ".".join(path.relative_to(PACKAGE_ROOT.parent).with_suffix("").parts)
        importlib.import_module(module.removesuffix(".__init__"))
