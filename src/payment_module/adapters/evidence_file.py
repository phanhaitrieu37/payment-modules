"""Evidence verifier reading provider verification JSON artifacts from one directory.

The artifact schema below is the package's own minimum; it has **not been verified against
the SePay Test verification output yet** (that run is deferred), so hosts must not enable
``auto_settle`` from hand-written files.

``evidence_ref`` is a path relative to ``root``. Absolute paths, ``..`` and symlinks that
leave the directory are rejected. The artifact (schema version 1)::

    {"schema_version": 1, "generated_at": "<ISO-8601 with offset>", "environment": "test",
     "accounts": [{"gateway": "VCB", "account_fingerprint": "VCB|0123456789|"}],
     "scenarios": {"a": {"status": "PASS"}, "b": {...}, "c": {...}, "d": {...}, "e": {...}},
     "digest": "<sha256 hex>"}

``digest`` is the SHA-256 of the canonical JSON (sorted keys, no spaces, UTF-8) of every
other field, so an artifact edited after it was produced is rejected. It is accepted only
when fresh (``max_age_days``), for the connection's environment, with scenario (a) ``PASS``
and every bound account's fingerprint listed.
"""

from __future__ import annotations

import hashlib
import json
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
    NOT_FOUND,
    PATH_OUTSIDE_ROOT,
    SCENARIO_A_NOT_PASS,
    STALE,
    EvidenceReport,
)
from payment_module.ports.provider import ReceivingAccountView
from payment_module.ports.resolvers import ProviderConnection

SCHEMA_VERSION = 1
SCENARIOS = ("a", "b", "c", "d", "e")
PASS = "PASS"


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
        if artifact.get("schema_version") != SCHEMA_VERSION:
            raise EvidenceRejected(BAD_SCHEMA, "unsupported schema_version")
        if artifact.get("digest") != artifact_digest(artifact):
            raise EvidenceRejected(BAD_DIGEST)
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
        covered = _covered_fingerprints(artifact.get("accounts"))
        needed = [account_key(a.bank_code, a.account_number, a.sub_account) for a in accounts]
        if not needed or any(fingerprint not in covered for fingerprint in needed):
            raise EvidenceRejected(ACCOUNT_NOT_COVERED)
        return EvidenceReport(
            evidence_ref=evidence_ref,
            environment=connection.environment,
            generated_at=generated_at,
            account_fingerprints=tuple(sorted(needed)),
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
        except ValueError as exc:
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


def _covered_fingerprints(accounts: object) -> frozenset[str]:
    if not isinstance(accounts, list):
        raise EvidenceRejected(BAD_SCHEMA, "accounts must be a list")
    fingerprints: set[str] = set()
    for entry in accounts:
        fingerprint = entry.get("account_fingerprint") if isinstance(entry, Mapping) else None
        gateway = entry.get("gateway") if isinstance(entry, Mapping) else None
        if not isinstance(fingerprint, str) or not isinstance(gateway, str):
            raise EvidenceRejected(BAD_SCHEMA, "each account needs gateway and fingerprint")
        fingerprints.add(fingerprint)
    return frozenset(fingerprints)
