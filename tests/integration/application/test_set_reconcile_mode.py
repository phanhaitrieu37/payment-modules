"""SetReconcileMode: ``auto_settle`` only with phase 03 evidence that covers the connection."""

from __future__ import annotations

import copy
import json
import shutil
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
EXAMPLE = Path(__file__).parents[2] / "fixtures" / "sepay-test-verification.example.json"


def gateway_result(**over: Any) -> dict[str, Any]:
    return {
        "pairs_total": 20,
        "pairs_equal_nonempty": 20,
        "pairs_mismatch": 0,
        "pairs_empty": 0,
        "auto_settle_eligible": True,
    } | over


def scenarios(**by_gateway: dict[str, Any]) -> dict[str, Any]:
    a = {
        "status": "PASS",
        "reason": "20 pairs",
        "by_gateway": by_gateway or {"VCB": gateway_result()},
    }
    return {"a": a} | {name: {"status": "PASS"} for name in "bcde"}


def artifact(app: App, **over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "evidence_schema_version": 1,
        "analyzer_version": "sepay_probe.analyze/1",
        "run_id": "run-1",
        "generated_at": (app.clock.now() - timedelta(days=1)).isoformat(),
        "environment": "test",
        "scenarios": scenarios(),
    } | over
    data["digest"] = artifact_digest(data)
    return data


def with_gateway(app: App, **over: Any) -> dict[str, Any]:
    return artifact(app, scenarios=scenarios(VCB=gateway_result(**over)))


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
        results = scenarios()
        results["a"]["status"] = status
        ref = write(tmp_path, f"a-{status}.json", artifact(app, scenarios=results))
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


async def test_the_canonical_example_artifact_is_accepted(
    app: App, ops: PaymentModule, tmp_path: Path
) -> None:
    shutil.copy(EXAMPLE, tmp_path / "example.json")

    connection = await set_mode(ops, app, ReconcileMode.AUTO_SETTLE, "example.json")

    assert connection.reconcile_mode == ReconcileMode.AUTO_SETTLE


async def test_gateway_is_matched_like_an_account_key(
    app: App, ops: PaymentModule, tmp_path: Path
) -> None:
    ref = write(tmp_path, "evidence.json", artifact(app, scenarios=scenarios(vcb=gateway_result())))
    connection = await set_mode(ops, app, ReconcileMode.AUTO_SETTLE, ref)
    assert connection.reconcile_mode == ReconcileMode.AUTO_SETTLE


def tampered(app: App) -> dict[str, Any]:
    data = artifact(app)
    data["scenarios"]["a"]["by_gateway"]["VCB"]["pairs_equal_nonempty"] = 40
    return data


def pass_but_not_eligible(app: App) -> dict[str, Any]:
    """Scenario (a) passed, but the run did not declare the bound gateway eligible."""
    return with_gateway(app, auto_settle_eligible=False)


def without(app: App, field: str) -> dict[str, Any]:
    data = copy.deepcopy(artifact(app))
    del data[field]
    data["digest"] = artifact_digest(data)
    return data


@pytest.mark.parametrize(
    ("build", "code"),
    [
        (tampered, "bad_digest"),
        (lambda app: artifact(app, evidence_schema_version=2), "bad_schema"),
        (lambda app: without(app, "evidence_schema_version"), "bad_schema"),
        (lambda app: artifact(app, analyzer_version="hand-written"), "unsupported_analyzer"),
        (lambda app: without(app, "analyzer_version"), "unsupported_analyzer"),
        (lambda app: artifact(app, run_id=""), "bad_schema"),
        (lambda app: without(app, "run_id"), "bad_schema"),
        (lambda app: without(app, "environment"), "environment_mismatch"),
        (
            lambda app: artifact(app, scenarios={"a": scenarios()["a"]}),
            "bad_schema",
        ),
        (pass_but_not_eligible, "gateway_not_eligible"),
        (lambda app: with_gateway(app, pairs_mismatch=1), "gateway_not_eligible"),
        (lambda app: with_gateway(app, pairs_equal_nonempty=19), "gateway_not_eligible"),
        (lambda app: with_gateway(app, auto_settle_eligible="true"), "bad_schema"),
        (lambda app: with_gateway(app, pairs_total=True), "bad_schema"),
        (lambda app: with_gateway(app, pairs_empty=-1), "bad_schema"),
        (
            lambda app: artifact(
                app, scenarios=scenarios() | {"a": {"status": "PASS", "reason": "no gateways"}}
            ),
            "bad_schema",
        ),
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
        (
            lambda app: artifact(app, scenarios=scenarios(MB=gateway_result())),
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
        app.m1.tenant_id, app.m1.merchant_id, app.m1.environment, "MB", "5550001", "SHOP", ACTOR
    )
    await ops.bind_connection_account.execute(
        app.m1.tenant_id, app.m1.connection_id, extra.id, ACTOR
    )
    ref = write(tmp_path, "evidence.json", artifact(app))
    with pytest.raises(EvidenceRejected) as caught:
        await set_mode(ops, app, ReconcileMode.AUTO_SETTLE, ref)
    assert caught.value.code == "account_not_covered"

    mb_not_eligible = scenarios(VCB=gateway_result(), MB=gateway_result(auto_settle_eligible=False))
    ref = write(tmp_path, "mb-not-eligible.json", artifact(app, scenarios=mb_not_eligible))
    with pytest.raises(EvidenceRejected) as caught:
        await set_mode(ops, app, ReconcileMode.AUTO_SETTLE, ref)
    assert caught.value.code == "gateway_not_eligible"
    assert await mode(app) == ("detect_only", None)

    both = scenarios(VCB=gateway_result(), MB=gateway_result())
    ref = write(tmp_path, "both.json", artifact(app, scenarios=both))
    connection = await set_mode(ops, app, ReconcileMode.AUTO_SETTLE, ref)
    assert connection.reconcile_mode == ReconcileMode.AUTO_SETTLE


async def test_database_requires_evidence_for_auto_settle(app: App) -> None:
    t = app.tables.provider_connections
    with pytest.raises(IntegrityError) as caught:
        await app.execute(
            sa.update(t)
            .where(t.c.id == app.m1.connection_id)
            .values(reconcile_mode="auto_settle", reconcile_evidence_ref=None)
        )
    assert "auto_settle_evidence_ck" in str(caught.value)
