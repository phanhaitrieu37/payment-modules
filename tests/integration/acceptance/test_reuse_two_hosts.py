"""Case "Reuse": one wheel installed into two clean virtual environments, one per example
host (option A ``saas_host`` with FastAPI, option B ``fnb_host`` without it), each host on a
database of its own; both smokes onboard, take a signed payment and fulfil it.

The wheel is built into a temporary directory, never the repository's ``dist/``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.acceptance,
    pytest.mark.timeout(300),
]

REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLES = REPO_ROOT / "examples"
EVIDENCE = REPO_ROOT / "tests" / "fixtures" / "sepay-test-verification.example.json"
EXTRAS = {"saas": "sqlalchemy,postgres,sepay,fastapi", "fnb": "sqlalchemy,postgres,sepay"}


def _run(*command: str | Path, **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(part) for part in command],
        capture_output=True,
        text=True,
        timeout=240,
        **kwargs,
    )


def _ok(*command: str | Path, **kwargs) -> str:
    result = _run(*command, **kwargs)
    assert result.returncode == 0, f"{command}\n{result.stdout}\n{result.stderr}"
    return result.stdout


@dataclass
class Venv:
    python: Path

    def package_dir(self) -> Path:
        code = "import pathlib, payment_module; print(pathlib.Path(payment_module.__file__).parent)"
        return Path(_ok(self.python, "-c", code).strip())

    def version(self) -> str:
        code = "from importlib.metadata import version; print(version('payment-module'))"
        return _ok(self.python, "-c", code).strip()

    def source_digests(self) -> dict[str, str]:
        root = self.package_dir()
        return {
            str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*.py"))
        }


@dataclass
class Installed:
    wheel: Path
    venvs: dict[str, Venv]


@pytest.fixture(scope="module")
def installed(tmp_path_factory: pytest.TempPathFactory) -> Installed:
    uv = shutil.which("uv")
    assert uv is not None, "uv is required to build the wheel and create the environments"
    root = tmp_path_factory.mktemp("reuse")
    _ok(uv, "build", "--wheel", "--out-dir", root / "wheel", cwd=REPO_ROOT)
    [wheel] = sorted((root / "wheel").glob("*.whl"))
    env = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}
    venvs = {}
    for host, extras in EXTRAS.items():
        _ok(uv, "venv", root / host, "--python", "3.12", env=env)
        python = root / host / "bin" / "python"
        requirement = f"payment-module[{extras}] @ {wheel.as_uri()}"
        _ok(uv, "pip", "install", "--python", python, requirement, env=env)
        venvs[host] = Venv(python)
    return Installed(wheel, venvs)


def test_the_wheel_holds_only_the_package(installed: Installed) -> None:
    names = zipfile.ZipFile(installed.wheel).namelist()
    assert not [name for name in names if name.startswith(("tests/", "examples/"))]
    assert "payment_module/adapters/fastapi/router.py" in names


def test_both_hosts_install_the_same_package(installed: Installed) -> None:
    saas, fnb = installed.venvs["saas"], installed.venvs["fnb"]
    assert saas.version() == fnb.version() == "0.1.0.dev0"
    assert saas.source_digests() == fnb.source_digests()
    assert _run(fnb.python, "-c", "import fastapi").returncode != 0
    assert _run(saas.python, "-c", "import fastapi").returncode == 0


def _smoke(venv: Venv, module: str, workdir: Path, **env: str) -> dict:
    environment = {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}
    environment |= {"PYTHONPATH": str(EXAMPLES), "SEPAY_WEBHOOK_SECRET": "whsec-reuse"} | env
    code = "import payment_module; print(payment_module.__file__)"
    where = _ok(venv.python, "-c", code, env=environment, cwd=workdir)
    assert Path(where.strip()).is_relative_to(venv.python.parent.parent), where
    output = _ok(venv.python, "-m", module, env=environment, cwd=workdir)
    return json.loads(output.strip().splitlines()[-1])


async def _tables(url: str) -> set[str]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return set(await conn.run_sync(lambda sync: sa.inspect(sync).get_table_names()))
    finally:
        await engine.dispose()


async def test_both_hosts_settle_on_their_own_database(
    installed: Installed, create_database, tmp_path: Path
) -> None:
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    shutil.copy(EVIDENCE, evidence_root / EVIDENCE.name)
    saas_url, fnb_url = await create_database("saas_host"), await create_database("fnb_host")

    saas = _smoke(
        installed.venvs["saas"],
        "saas_host.app",
        tmp_path,
        DATABASE_URL=saas_url,
        EVIDENCE_ROOT=str(evidence_root),
    )
    fnb = _smoke(installed.venvs["fnb"], "fnb_host.main", tmp_path, DATABASE_URL=fnb_url)

    assert saas == {
        "package_version": "0.1.0.dev0",
        "intent_status_before_payment": "awaiting_payment",
        "replay_same_intent": True,
        "webhook_status": 200,
        "order": {"status": "paid", "last_outcome": "settled"},
        "legacy_order_status": "paid",
        "rematched": ["settled"],
        "reconcile_modes": ["auto_settle", "detect_only"],
    }
    assert fnb == {
        "package_version": "0.1.0.dev0",
        "fastapi_installed": False,
        "intent_status_before_payment": "awaiting_payment",
        "ingest_status": "accepted",
        "intent_status_after_payment": "paid",
        "receipts": 1,
        "bill_paid_by_event": True,
        "event": {"event_type": "PaymentSettled", "schema_version": 1, "amount_vnd": 320_000},
    }
    saas_tables, fnb_tables = await _tables(saas_url), await _tables(fnb_url)
    assert {"orders", "pm_settlements"} <= saas_tables and "table_bills" not in saas_tables
    assert {"table_bills", "pm_settlements"} <= fnb_tables and "orders" not in fnb_tables
