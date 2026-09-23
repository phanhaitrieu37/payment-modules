from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

VERSION = "sepay_probe.analyze/1"


def _load_api(path):
    if not path or not Path(path).exists():
        return []
    data = json.loads(Path(path).read_text())
    return data.get("data", data if isinstance(data, list) else [])


def _captures(path):
    p = Path(path) / "capture.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def artifact_digest(a):
    rest = {k: v for k, v in a.items() if k != "digest"}
    return hashlib.sha256(
        json.dumps(rest, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _tokens(v):
    if not isinstance(v, str):
        return set()
    return {x for x in v.replace("|", " ").replace(",", " ").split() if x}


def analyze(capture_dir, api_path=None, run_id=None):
    caps = _captures(capture_dir)
    api = _load_api(api_path)
    valid = [c for c in caps if c.get("hmac_valid") and isinstance(c.get("payload"), dict)]
    by_amount = {}
    for c in valid:
        p = c["payload"]
        amount = p.get("transferAmount", p.get("amount"))
        if amount is not None:
            by_amount.setdefault(str(amount), []).append(p)
    api_by_amount = {}
    for row in api:
        amount = row.get("amount", row.get("amount_in", row.get("amount_out")))
        if amount is not None:
            api_by_amount.setdefault(str(amount), []).append(row)
    gateways = {}
    for amount, ws in by_amount.items():
        if len(ws) != 1 or len(api_by_amount.get(amount, [])) != 1:
            continue
        w = ws[0]
        a = api_by_amount[amount][0]
        gateway = str(w.get("gateway", "UNKNOWN"))
        refw = w.get("referenceCode") or w.get("reference_code") or ""
        refa = a.get("reference_number") or ""
        g = gateways.setdefault(
            gateway,
            {"pairs_total": 0, "pairs_equal_nonempty": 0, "pairs_mismatch": 0, "pairs_empty": 0},
        )
        g["pairs_total"] += 1
        if not refw or not refa:
            g["pairs_empty"] += 1
        elif refw == refa:
            g["pairs_equal_nonempty"] += 1
        else:
            g["pairs_mismatch"] += 1
    for g in gateways.values():
        g["auto_settle_eligible"] = (
            g["pairs_total"] >= 20
            and g["pairs_equal_nonempty"] >= 20
            and g["pairs_mismatch"] == 0
            and g["pairs_empty"] == 0
        )
    astatus = (
        "PASS"
        if gateways and any(x["auto_settle_eligible"] for x in gateways.values())
        else ("INCONCLUSIVE" if gateways else "NOT_RUN")
    )
    scenarios = {
        "a": {
            "status": astatus,
            "reason": "Synthetic or captured HMAC-valid unique-amount pairs analyzed",
            "by_gateway": gateways,
            "content_equal": bool(valid and api)
            and (
                {t for c in valid for t in _tokens(c["payload"].get("content"))}
                == {t for x in api for t in _tokens(x.get("content") or x.get("reference_number"))}
            ),
        },
        "b": {
            "status": "NOT_RUN",
            "reason": "Retry outage observation not supplied",
            "timestamp_on_retry": "unknown",
            "attempts": [],
            "recommended_tolerance_seconds": 300,
        },
        "c": {
            "status": "NOT_RUN",
            "reason": "Outgoing transfer observation not supplied",
            "webhook_transfer_type_values": [],
            "api_direction_fields": {},
        },
        "d": {
            "status": "NOT_RUN",
            "reason": "Filtered no-code transaction observation not supplied",
            "webhook_received": False,
            "webhook_success_values": [],
        },
        "e": {
            "status": "NOT_RUN",
            "reason": "Virtual-account and primary-account observations not supplied",
            "account_key_rule": "bank|account_number|sub_account",
            "rule_confirmed": False,
            "observed_fields": {},
        },
    }
    return {
        "evidence_schema_version": 1,
        "analyzer_version": VERSION,
        "run_id": run_id or Path(capture_dir).name,
        "environment": "test",
        "generated_at": datetime.now(UTC).isoformat(),
        "scenarios": scenarios,
    }


def write_outputs(capture_dir, out, report=None, api_path=None, run_id=None):
    a = analyze(capture_dir, api_path, run_id)
    a["digest"] = artifact_digest(a)
    Path(out).write_text(json.dumps(a, indent=2, ensure_ascii=False) + "\n")
    if report:
        lines = [
            "# SePay Test verification",
            "",
            f"Run: `{a['run_id']}`; environment: `test`.",
            "",
            "| Scenario | Status | Reason |",
            "|---|---|---|",
        ]
        for k, v in a["scenarios"].items():
            lines.append(f"| {k} | {v['status']} | {v['reason']} |")
        Path(report).write_text("\n".join(lines) + "\n")
    return a


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--capture-dir", required=True)
    p.add_argument("--api")
    p.add_argument("--out", required=True)
    p.add_argument("--report")
    p.add_argument("--run-id")
    x = p.parse_args()
    write_outputs(x.capture_dir, x.out, x.report, x.api, x.run_id)
