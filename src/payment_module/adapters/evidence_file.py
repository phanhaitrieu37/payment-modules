"""Evidence verifier reading SePay Test verification JSON artifacts from one directory.

The artifact is the conclusion JSON the phase 03 analyzer (``tools/sepay_probe/analyze.py``)
emits; ``tests/fixtures/sepay-test-verification.example.json`` is its canonical example::

    {"evidence_schema_version": 1, "analyzer_version": "sepay_probe.analyze/1",
     "run_id": "...", "environment": "test", "generated_at": "<ISO-8601 with offset>",
     "scenarios": {"a": {"status": "PASS", "reason": "...", "by_gateway": {"<gateway>": {
                          "pairs_total": 20, "pairs_equal_nonempty": 20, "pairs_mismatch": 0,
                          "pairs_empty": 0, "auto_settle_eligible": true}}},
                   "b": {...}, "c": {...}, "d": {...}, "e": {...}},
     "digest": "<sha256 hex>"}

``evidence_ref`` is a path relative to ``root``. Absolute paths, ``..`` and symlinks that
leave the directory are rejected. ``digest`` is the SHA-256 of the canonical JSON (sorted keys,
no spaces, UTF-8) of every other field; it detects an artifact edited after it was produced,
not who produced it.

An artifact is accepted only when fresh (``max_age_days``), from a supported analyzer, for the
connection's environment, with scenario (a) ``PASS`` and, for the gateway of every bound
account, ``auto_settle_eligible`` true, no mismatched pair and at least
:data:`MIN_EQUAL_PAIRS` pairs whose bank references are equal and non-empty. A scenario (a)
``PASS`` alone proves nothing for a gateway the run did not declare eligible.

Every field is type-checked before use and each gateway's counts must be consistent (equal,
mismatched and empty pairs are disjoint categories of ``pairs_total``), so a malformed or
contradictory artifact is always an :class:`EvidenceRejected`, never an authorization or a
``TypeError``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from payment_module.domain.account_identity import account_key
from payment_module.domain.errors import EvidenceRejected
from payment_module.ports.clock import Clock
from payment_module.ports.evidence import (
    ACCOUNT_NOT_COVERED,
    BAD_DIGEST,
    BAD_SCHEMA,
    ENVIRONMENT_MISMATCH,
    GATEWAY_NOT_ELIGIBLE,
    NOT_FOUND,
    PATH_OUTSIDE_ROOT,
    SCENARIO_A_NOT_PASS,
    STALE,
    UNSUPPORTED_ANALYZER,
    EvidenceReport,
)
from payment_module.ports.provider import ReceivingAccountView
from payment_module.ports.resolvers import ProviderConnection

EVIDENCE_SCHEMA_VERSION = 1
SUPPORTED_ANALYZER_VERSIONS = frozenset({"sepay_probe.analyze/1"})
SCENARIOS = ("a", "b", "c", "d", "e")
PASS = "PASS"
MIN_EQUAL_PAIRS = 20
_COUNTS = ("pairs_total", "pairs_equal_nonempty", "pairs_mismatch", "pairs_empty")
_WHITESPACE = re.compile(r"\s+")


def artifact_digest(artifact: Mapping[str, Any]) -> str:
    """SHA-256 of the canonical JSON of ``artifact`` without its ``digest`` field."""
    rest = {key: value for key, value in artifact.items() if key != "digest"}
    canonical = json.dumps(rest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


class FileEvidenceVerifier:
    def __init__(self, root: Path, clock: Clock, *, max_age_days: int = 180) -> None:
        self._root = Path(root).resolve()
        self._clock = clock
        self._max_age = timedelta(days=max_age_days)

    def verify(
        self,
        evidence_ref: str,
        *,
        connection: ProviderConnection,
        accounts: Sequence[ReceivingAccountView],
    ) -> EvidenceReport:
        artifact = self._load(evidence_ref)
        schema_version = artifact.get("evidence_schema_version")
        if type(schema_version) is not int or schema_version != EVIDENCE_SCHEMA_VERSION:
            raise EvidenceRejected(BAD_SCHEMA, "unsupported evidence_schema_version")
        if artifact.get("digest") != artifact_digest(artifact):
            raise EvidenceRejected(BAD_DIGEST)
        analyzer_version = artifact.get("analyzer_version")
        if (
            not isinstance(analyzer_version, str)
            or analyzer_version not in SUPPORTED_ANALYZER_VERSIONS
        ):
            raise EvidenceRejected(UNSUPPORTED_ANALYZER)
        run_id = artifact.get("run_id")
        if not isinstance(run_id, str) or not run_id.strip():
            raise EvidenceRejected(BAD_SCHEMA, "run_id is required")
        generated_at = self._generated_at(artifact.get("generated_at"))
        if artifact.get("environment") != connection.environment.value:
            raise EvidenceRejected(ENVIRONMENT_MISMATCH)
        scenarios = artifact.get("scenarios")
        if not isinstance(scenarios, Mapping) or not all(
            isinstance(scenarios.get(name), Mapping) for name in SCENARIOS
        ):
            raise EvidenceRejected(BAD_SCHEMA, "scenarios a to e are required")
        if scenarios["a"].get("status") != PASS:
            raise EvidenceRejected(SCENARIO_A_NOT_PASS)
        gateways = _gateways(scenarios["a"].get("by_gateway"))
        if not accounts:
            raise EvidenceRejected(ACCOUNT_NOT_COVERED, "the connection has no bound account")
        for account in accounts:
            result = gateways.get(_gateway_key(account.bank_code))
            if result is None:
                raise EvidenceRejected(ACCOUNT_NOT_COVERED)
            if not _eligible(result):
                raise EvidenceRejected(GATEWAY_NOT_ELIGIBLE)
        fingerprints = (account_key(a.bank_code, a.account_number, a.sub_account) for a in accounts)
        return EvidenceReport(
            evidence_ref=evidence_ref,
            environment=connection.environment,
            generated_at=generated_at,
            account_fingerprints=tuple(sorted(fingerprints)),
            run_id=run_id,
            analyzer_version=analyzer_version,
        )

    def _load(self, evidence_ref: str) -> Mapping[str, Any]:
        relative = PurePosixPath(evidence_ref or "")
        if not evidence_ref or relative.is_absolute() or ".." in relative.parts:
            raise EvidenceRejected(PATH_OUTSIDE_ROOT)
        path = (self._root / relative).resolve()
        if not path.is_relative_to(self._root):
            raise EvidenceRejected(PATH_OUTSIDE_ROOT)
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise EvidenceRejected(NOT_FOUND) from exc
        try:
            artifact = json.loads(raw)
        except (ValueError, RecursionError) as exc:
            raise EvidenceRejected(BAD_SCHEMA, "artifact is not JSON") from exc
        if not isinstance(artifact, dict):
            raise EvidenceRejected(BAD_SCHEMA, "artifact is not a JSON object")
        return artifact

    def _generated_at(self, value: object) -> datetime:
        try:
            generated_at = datetime.fromisoformat(value) if isinstance(value, str) else None
        except ValueError:
            generated_at = None
        if generated_at is None or generated_at.tzinfo is None:
            raise EvidenceRejected(BAD_SCHEMA, "generated_at must be an aware ISO-8601 time")
        age = self._clock.now() - generated_at
        if age < timedelta(0) or age > self._max_age:
            raise EvidenceRejected(STALE)
        return generated_at


def _gateway_key(gateway: str) -> str:
    """Gateways compare like the bank part of an account key: no whitespace, upper case."""
    return _WHITESPACE.sub("", gateway).upper()


def _gateways(by_gateway: object) -> dict[str, Mapping[str, Any]]:
    """Scenario (a)'s per-gateway results keyed by :func:`_gateway_key`, shape-checked."""
    if not isinstance(by_gateway, Mapping):
        raise EvidenceRejected(BAD_SCHEMA, "scenario a needs by_gateway")
    gateways: dict[str, Mapping[str, Any]] = {}
    for gateway, result in by_gateway.items():
        key = _gateway_key(gateway) if isinstance(gateway, str) else ""
        if not key or key in gateways or not isinstance(result, Mapping):
            raise EvidenceRejected(BAD_SCHEMA, "by_gateway entries must be distinct objects")
        counts = [result.get(name) for name in _COUNTS]
        if not all(type(count) is int and count >= 0 for count in counts):
            raise EvidenceRejected(BAD_SCHEMA, "pair counts must be non-negative integers")
        total, equal, mismatch, empty = counts
        if equal + mismatch + empty > total:
            raise EvidenceRejected(BAD_SCHEMA, "pair counts exceed pairs_total")
        if not isinstance(result.get("auto_settle_eligible"), bool):
            raise EvidenceRejected(BAD_SCHEMA, "auto_settle_eligible must be a boolean")
        gateways[key] = result
    return gateways


def _eligible(result: Mapping[str, Any]) -> bool:
    return (
        result["auto_settle_eligible"] is True
        and result["pairs_mismatch"] == 0
        and result["pairs_equal_nonempty"] >= MIN_EQUAL_PAIRS
    )
