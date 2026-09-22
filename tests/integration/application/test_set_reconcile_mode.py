"""SetReconcileMode: ``auto_settle`` only with evidence that covers the connection."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from fakes.payment_app import App
from payment_module.adapters.evidence_file import FileEvidenceVerifier, artifact_digest
from payment_module.builder import PaymentModule
from payment_module.domain.enums import ReconcileMode
from payment_module.domain.errors import EvidenceRejected

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

ACTOR = "ops@example.test"


def artifact(app: App, **over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": (app.clock.now() - timedelta(days=1)).isoformat(),
        "environment": "test",
        "accounts": [
            {"gateway": "VCB", "account_fingerprint": f"VCB|{app.m1.account_number}|"},
        ],
        "scenarios": {name: {"status": "PASS"} for name in "abcde"},
    } | over
    data["digest"] = artifact_digest(data)
    return data


def write(root: Path, name: str, data: dict[str, Any]) -> str:
    (root / name).write_text(json.dumps(data))
    return name


@pytest.fixture
def ops(app: App, tmp_path: Path) -> PaymentModule:
    return app.build(evidence_verifier=FileEvidenceVerifier(tmp_path, app.clock, max_age_days=30))


async def mode(app: App) -> tuple[str, str | None]:
    t = app.tables.provider_connections
    [row] = await app.rows(t, t.c.id == app.m1.connection_id)
    return row.reconcile_mode, row.reconcile_evidence_ref


async def set_mode(module: PaymentModule, app: App, mode_: ReconcileMode, ref: str | None):
    return await module.set_reconcile_mode.execute(
        app.m1.tenant_id, app.m1.connection_id, mode_, ref, ACTOR
    )


async def test_auto_settle_without_a_verifier_is_refused(app: App) -> None:
    with pytest.raises(EvidenceRejected) as caught:
        await set_mode(app.module, app, ReconcileMode.AUTO_SETTLE, "evidence.json")
    assert caught.value.code == "verifier_not_configured"
    assert await mode(app) == ("detect_only", None)


async def test_auto_settle_without_evidence_is_refused(app: App, ops: PaymentModule) -> None:
    with pytest.raises(EvidenceRejected) as caught:
        await set_mode(ops, app, ReconcileMode.AUTO_SETTLE, None)
    assert caught.value.code == "not_found"
    assert await mode(app) == ("detect_only", None)


async def test_scenario_a_must_pass(app: App, ops: PaymentModule, tmp_path: Path) -> None:
    for status in ("FAIL", "INCONCLUSIVE", "NOT_RUN"):
        scenarios = {name: {"status": "PASS"} for name in "bcde"} | {"a": {"status": status}}
        ref = write(tmp_path, f"a-{status}.json", artifact(app, scenarios=scenarios))
        with pytest.raises(EvidenceRejected) as caught:
            await set_mode(ops, app, ReconcileMode.AUTO_SETTLE, ref)
        assert caught.value.code == "scenario_a_not_pass"
    assert await mode(app) == ("detect_only", None)


async def test_valid_evidence_enables_auto_settle_and_detect_only_clears_it(
    app: App, ops: PaymentModule, tmp_path: Path
) -> None:
    ref = write(tmp_path, "sepay-test-verification.json", artifact(app))

    connection = await set_mode(ops, app, ReconcileMode.AUTO_SETTLE, ref)

    assert connection.reconcile_mode == ReconcileMode.AUTO_SETTLE
    assert await mode(app) == ("auto_settle", ref)
    await set_mode(ops, app, ReconcileMode.DETECT_ONLY, None)
    assert await mode(app) == ("detect_only", None)


def tampered(app: App) -> dict[str, Any]:
    data = artifact(app)
    data["accounts"].append({"gateway": "VCB", "account_fingerprint": "VCB|999|"})
    return data


@pytest.mark.parametrize(
    ("build", "code"),
    [
        (tampered, "bad_digest"),
        (lambda app: artifact(app, schema_version=2), "bad_schema"),
        (lambda app: artifact(app, scenarios={"a": {"status": "PASS"}}), "bad_schema"),
        (lambda app: artifact(app, environment="live"), "environment_mismatch"),
        (
            lambda app: artifact(
                app, generated_at=(app.clock.now() - timedelta(days=31)).isoformat()
            ),
            "stale",
        ),
        (
            lambda app: artifact(
                app, generated_at=(app.clock.now() + timedelta(days=1)).isoformat()
            ),
            "stale",
        ),
        (lambda app: artifact(app, generated_at="2026-09-01T00:00:00"), "bad_schema"),
        (lambda app: artifact(app, accounts=[]), "account_not_covered"),
        (
            lambda app: artifact(
                app, accounts=[{"gateway": "VCB", "account_fingerprint": "VCB|000|"}]
            ),
            "account_not_covered",
        ),
    ],
)
async def test_evidence_that_does_not_prove_the_scope_is_refused(
    app: App, ops: PaymentModule, tmp_path: Path, build, code: str
) -> None:
    ref = write(tmp_path, "evidence.json", build(app))
    with pytest.raises(EvidenceRejected) as caught:
        await set_mode(ops, app, ReconcileMode.AUTO_SETTLE, ref)
    assert caught.value.code == code
    assert await mode(app) == ("detect_only", None)


@pytest.mark.parametrize(
    ("ref", "code"),
    [
        ("../outside.json", "path_outside_root"),
        ("/etc/passwd", "path_outside_root"),
        ("missing.json", "not_found"),
    ],
)
async def test_evidence_outside_the_directory_is_refused(
    app: App, ops: PaymentModule, tmp_path: Path, ref: str, code: str
) -> None:
    (tmp_path.parent / "outside.json").write_text(json.dumps(artifact(app)))
    with pytest.raises(EvidenceRejected) as caught:
        await set_mode(ops, app, ReconcileMode.AUTO_SETTLE, ref)
    assert caught.value.code == code


async def test_symlink_leaving_the_directory_is_refused(
    app: App, ops: PaymentModule, tmp_path: Path
) -> None:
    root = tmp_path / "evidence"
    root.mkdir()
    (tmp_path / "real.json").write_text(json.dumps(artifact(app)))
    (root / "link.json").symlink_to(tmp_path / "real.json")
    module = app.build(evidence_verifier=FileEvidenceVerifier(root, app.clock))

    with pytest.raises(EvidenceRejected) as caught:
        await set_mode(module, app, ReconcileMode.AUTO_SETTLE, "link.json")
    assert caught.value.code == "path_outside_root"


async def test_every_bound_account_must_be_covered(
    app: App, ops: PaymentModule, tmp_path: Path
) -> None:
    extra = await ops.register_receiving_account.execute(
        app.m1.tenant_id, app.m1.merchant_id, app.m1.environment, "VCB", "5550001", "SHOP", ACTOR
    )
    await ops.bind_connection_account.execute(
        app.m1.tenant_id, app.m1.connection_id, extra.id, ACTOR
    )
    ref = write(tmp_path, "evidence.json", artifact(app))

    with pytest.raises(EvidenceRejected) as caught:
        await set_mode(ops, app, ReconcileMode.AUTO_SETTLE, ref)
    assert caught.value.code == "account_not_covered"


async def test_database_requires_evidence_for_auto_settle(app: App) -> None:
    t = app.tables.provider_connections
    with pytest.raises(IntegrityError) as caught:
        await app.execute(
            sa.update(t)
            .where(t.c.id == app.m1.connection_id)
            .values(reconcile_mode="auto_settle", reconcile_evidence_ref=None)
        )
    assert "auto_settle_evidence_ck" in str(caught.value)
