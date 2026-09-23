"""The committed SePay Test verdict must not enable ``auto_settle``; 20 equal pairs would.

The verdict saw five equal, non-empty bank reference pairs on one gateway: every pair agreed,
but five is below :data:`MIN_EQUAL_PAIRS`, so the analyzer left the gateway ineligible and
scenario (a) inconclusive.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from payment_module.adapters.evidence_file import (
    MIN_EQUAL_PAIRS,
    FileEvidenceVerifier,
    artifact_digest,
)
from payment_module.domain.enums import (
    ConnectionStatus,
    Environment,
    ReceivingAccountStatus,
    ReconcileMode,
)
from payment_module.domain.errors import EvidenceRejected
from payment_module.ports.evidence import GATEWAY_NOT_ELIGIBLE, SCENARIO_A_NOT_PASS
from payment_module.ports.provider import ReceivingAccountView
from payment_module.ports.resolvers import ProviderConnection

VERDICT = Path(__file__).parents[3] / "plans" / "reports" / "sepay-test-verification-260922.json"
GATEWAY = "ACB"


class FixedClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


def connection() -> ProviderConnection:
    return ProviderConnection(
        uuid4(),
        "tenant",
        uuid4(),
        Environment.TEST,
        "sepay",
        "locator",
        ConnectionStatus.ACTIVE,
        ReconcileMode.DETECT_ONLY,
        300,
        "secret-ref",
        None,
    )


def account(conn: ProviderConnection) -> ReceivingAccountView:
    return ReceivingAccountView(
        uuid4(),
        conn.tenant_id,
        conn.merchant_id,
        Environment.TEST,
        GATEWAY,
        "9990000001",
        None,
        "Test account",
        ReceivingAccountStatus.ACTIVE,
    )


def verify(root: Path, artifact: dict[str, Any], name: str = "verdict.json"):
    (root / name).write_text(json.dumps(artifact), encoding="utf-8")
    clock = FixedClock(datetime.fromisoformat(artifact["generated_at"]) + timedelta(days=1))
    conn = connection()
    return FileEvidenceVerifier(root, clock).verify(name, connection=conn, accounts=[account(conn)])


def redigest(artifact: dict[str, Any]) -> dict[str, Any]:
    artifact["digest"] = artifact_digest(artifact)
    return artifact


@pytest.fixture
def verdict() -> dict[str, Any]:
    artifact = json.loads(VERDICT.read_text(encoding="utf-8"))
    assert artifact["digest"] == artifact_digest(artifact)
    return artifact


def test_committed_verdict_is_rejected_for_auto_settle(tmp_path: Path, verdict) -> None:
    counts = verdict["scenarios"]["a"]["by_gateway"][GATEWAY]
    assert counts["pairs_equal_nonempty"] < MIN_EQUAL_PAIRS
    assert counts["auto_settle_eligible"] is False
    with pytest.raises(EvidenceRejected) as rejected:
        verify(tmp_path, verdict)
    assert rejected.value.code == SCENARIO_A_NOT_PASS


def test_too_few_pairs_are_rejected_even_when_scenario_a_claims_pass(
    tmp_path: Path, verdict
) -> None:
    verdict["scenarios"]["a"]["status"] = "PASS"
    verdict["scenarios"]["a"]["by_gateway"][GATEWAY]["auto_settle_eligible"] = True
    with pytest.raises(EvidenceRejected) as rejected:
        verify(tmp_path, redigest(verdict))
    assert rejected.value.code == GATEWAY_NOT_ELIGIBLE


def test_twenty_equal_pairs_on_the_gateway_are_accepted(tmp_path: Path, verdict) -> None:
    verdict["run_id"] = "synthetic-twenty-pairs"
    verdict["scenarios"]["a"]["status"] = "PASS"
    verdict["scenarios"]["a"]["by_gateway"] = {
        GATEWAY: {
            "pairs_total": MIN_EQUAL_PAIRS,
            "pairs_equal_nonempty": MIN_EQUAL_PAIRS,
            "pairs_mismatch": 0,
            "pairs_empty": 0,
            "auto_settle_eligible": True,
        }
    }
    report = verify(tmp_path, redigest(verdict))
    assert report.run_id == "synthetic-twenty-pairs"
    assert report.environment == Environment.TEST
