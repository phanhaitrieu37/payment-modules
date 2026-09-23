import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from tools.sepay_probe.analyze import analyze, artifact_digest


def test_analyzer_pairs_unique_amounts(tmp_path):
    payloads = []
    for i in range(20):
        payloads.append(
            {
                "received_at": "2026-09-22T01:00:00Z",
                "hmac_valid": True,
                "payload": {
                    "transferAmount": str(10001 + i),
                    "gateway": "VCB",
                    "referenceCode": f"R{i}",
                    "content": f"PAY R{i}",
                },
            }
        )
    (tmp_path / "capture.jsonl").write_text("\n".join(json.dumps(x) for x in payloads))
    api = {
        "data": [
            {"amount": str(10001 + i), "reference_number": f"R{i}", "content": f"PAY R{i}"}
            for i in range(20)
        ]
    }
    ap = tmp_path / "api.json"
    ap.write_text(json.dumps(api))
    result = analyze(tmp_path, ap, "run-test")
    assert result["scenarios"]["a"]["status"] == "PASS"
    assert result["scenarios"]["a"]["by_gateway"]["VCB"]["auto_settle_eligible"]


def test_digest_changes_on_mismatch():
    a = {
        "evidence_schema_version": 1,
        "analyzer_version": "sepay_probe.analyze/1",
        "run_id": "x",
        "environment": "test",
        "generated_at": "2026-01-01T00:00:00+00:00",
        "scenarios": {k: {"status": "NOT_RUN"} for k in "abcde"},
    }
    digest = artifact_digest(a)
    a["digest"] = digest
    a["scenarios"]["a"]["status"] = "PASS"
    assert artifact_digest(a) != digest


def test_file_evidence_verifier_accepts_artifact_and_rejects_mismatch(tmp_path):
    from datetime import UTC, datetime
    from uuid import uuid4

    from payment_module.adapters.evidence_file import FileEvidenceVerifier, artifact_digest
    from payment_module.domain.enums import (
        ConnectionStatus,
        Environment,
        ReceivingAccountStatus,
        ReconcileMode,
    )
    from payment_module.ports.provider import ReceivingAccountView
    from payment_module.ports.resolvers import ProviderConnection

    class Clock:
        def now(self):
            return datetime(2026, 9, 22, tzinfo=UTC)

    artifact = {
        "evidence_schema_version": 1,
        "analyzer_version": "sepay_probe.analyze/1",
        "run_id": "r",
        "environment": "test",
        "generated_at": "2026-09-22T00:00:00+00:00",
        "scenarios": {k: {"status": "NOT_RUN"} for k in "abcde"},
    }
    artifact["scenarios"]["a"] = {
        "status": "PASS",
        "by_gateway": {
            "VCB": {
                "pairs_total": 20,
                "pairs_equal_nonempty": 20,
                "pairs_mismatch": 0,
                "pairs_empty": 0,
                "auto_settle_eligible": True,
            }
        },
    }
    artifact["digest"] = artifact_digest(artifact)
    path = tmp_path / "e.json"
    path.write_text(json.dumps(artifact))
    conn = ProviderConnection(
        uuid4(),
        "t",
        uuid4(),
        Environment.TEST,
        "sepay",
        "loc",
        ConnectionStatus.ACTIVE,
        ReconcileMode.DETECT_ONLY,
        300,
        "s",
        None,
    )
    acc = ReceivingAccountView(
        uuid4(),
        "t",
        conn.merchant_id,
        Environment.TEST,
        "VCB",
        "123",
        None,
        "n",
        ReceivingAccountStatus.ACTIVE,
    )
    assert (
        FileEvidenceVerifier(tmp_path, Clock())
        .verify("e.json", connection=conn, accounts=[acc])
        .run_id
        == "r"
    )
    artifact["scenarios"]["a"]["by_gateway"]["VCB"]["pairs_mismatch"] = 1
    path.write_text(json.dumps(artifact))
    from payment_module.domain.errors import EvidenceRejected

    try:
        FileEvidenceVerifier(tmp_path, Clock()).verify("e.json", connection=conn, accounts=[acc])
    except EvidenceRejected:
        pass
    else:
        raise AssertionError("mismatched artifact must be rejected")


def test_capture_handler_requires_scheme_and_logs_payload_id(tmp_path):
    import hashlib
    import hmac
    import threading
    from http.client import HTTPConnection
    from http.server import ThreadingHTTPServer

    from tools.sepay_probe.capture_server import make_handler

    secret = "unit-secret"
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), make_handler(tmp_path, secret, tmp_path / "mode")
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body = b'{"id":"anon-1","transferAmount":"10001"}'
        ts = "1727000000"
        digest = hmac.new(secret.encode(), (ts + ".").encode() + body, hashlib.sha256).hexdigest()
        conn = HTTPConnection(*server.server_address)
        conn.request(
            "POST",
            "/sepay",
            body,
            {
                "Content-Length": str(len(body)),
                "X-SePay-Timestamp": ts,
                "X-SePay-Signature": "sha256=" + digest,
            },
        )
        assert conn.getresponse().status == 200
        record = json.loads((tmp_path / "capture.jsonl").read_text())
        assert record["hmac_valid"] is True
        assert record["id"] == "anon-1"
    finally:
        server.shutdown()
        thread.join()
