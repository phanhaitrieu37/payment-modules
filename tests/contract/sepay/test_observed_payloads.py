"""Normalization of payloads captured on SePay Test.

The fixtures under ``tests/fixtures/sepay`` are real SePay Test webhook bodies and API v2 rows
with account numbers, VA, ids and bank references replaced (``_provenance``). A webhook and
the API row of the same transfer keep every equality the real pair had.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from payment_module.adapters.sepay.payload import normalize_api_row
from payment_module.adapters.sepay.provider import SePayProvider
from payment_module.domain.enums import Direction, IdentityKind, ObservationSource
from payment_module.domain.reference import tokens_from
from payment_module.ports.provider import NormalizedObservation, VerifiedDelivery

pytestmark = pytest.mark.contract

FIXTURES = Path(__file__).parents[2] / "fixtures" / "sepay"
NOW = datetime(2026, 9, 22, 11, 25, tzinfo=UTC)
MAIN_KEY = "ACB|9990000001|"
VA_KEY = "ACB|9990000001|SBVAMASKED0001"


def load(name: str) -> dict[str, Any]:
    data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert data["_provenance"].startswith("observed")
    return data


def webhook(name: str) -> NormalizedObservation:
    raw = json.dumps(load(name)).encode()
    delivery = VerifiedDelivery(raw_body=raw, headers={}, verified_at=NOW)
    return SePayProvider().normalize(delivery)


def api_rows() -> dict[int, NormalizedObservation]:
    rows = load("api_transactions.json")["data"]
    return {row["amount_in"] or row["amount_out"]: normalize_api_row(row) for row in rows}


def test_inbound_main_account_webhook() -> None:
    obs = webhook("webhook_in.json")
    assert obs.source == ObservationSource.WEBHOOK
    assert obs.identity_kind == IdentityKind.WEBHOOK_ID
    assert obs.source_tx_id == "900009"
    assert obs.reported_account_key == MAIN_KEY
    assert obs.direction == Direction.IN
    assert obs.amount.value == 10_009
    assert obs.bank_reference == "SB000000010009"
    assert obs.code is None
    assert tokens_from(obs.code, obs.content) == ["TESTE02"]


def test_inbound_va_webhook_keys_the_va_under_the_main_account_number() -> None:
    obs = webhook("webhook_in_va.json")
    assert obs.reported_account_key == VA_KEY
    assert obs.direction == Direction.IN
    assert obs.amount.value == 10_008
    assert obs.bank_reference == "SB000000010008"
    assert tokens_from(obs.code, obs.content) == ["TESTE01"]


def test_outgoing_api_row() -> None:
    obs = api_rows()[10_007]
    assert obs.source == ObservationSource.API
    assert obs.identity_kind == IdentityKind.API_ID
    assert obs.direction == Direction.OUT
    assert obs.amount.value == 10_007
    assert obs.reported_account_key == MAIN_KEY
    assert obs.bank_reference == "SB000000010007"
    assert tokens_from(obs.code, obs.content) == ["TESTC01"]


@pytest.mark.parametrize(
    ("name", "amount", "account_key"),
    [("webhook_in.json", 10_009, MAIN_KEY), ("webhook_in_va.json", 10_008, VA_KEY)],
)
def test_webhook_and_api_row_of_one_transfer_agree(
    name: str, amount: int, account_key: str
) -> None:
    hook, row = webhook(name), api_rows()[amount]
    assert row.reported_account_key == hook.reported_account_key == account_key
    assert row.direction == hook.direction == Direction.IN
    assert row.amount == hook.amount
    assert row.bank_reference == hook.bank_reference
    assert tokens_from(row.code, row.content) == tokens_from(hook.code, hook.content)
    # Numeric webhook ids and UUID API ids are separate spaces and never compared.
    assert hook.source_tx_id.isdigit()
    assert not row.source_tx_id.isdigit()
