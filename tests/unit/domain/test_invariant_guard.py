from __future__ import annotations

from uuid import uuid4

import pytest

from payment_module.domain.enums import Direction, Environment
from payment_module.domain.matching.invariant_guard import GuardResult, InvariantGuard


def test_incoming_to_bound_account_passes(make_tx, connection, account_id) -> None:
    assert InvariantGuard().check(make_tx(), {account_id}, connection) is GuardResult.PASS


@pytest.mark.parametrize("direction", [Direction.OUT, Direction.UNKNOWN])
@pytest.mark.parametrize("receiver_bound", [True, False])
def test_outgoing_or_unknown_is_checked_before_receiver(
    make_tx, connection, account_id, direction: Direction, receiver_bound: bool
) -> None:
    tx = make_tx(
        direction=direction,
        receiving_account_id=account_id if receiver_bound else None,
        merchant_id=connection.merchant_id if receiver_bound else None,
    )
    assert InvariantGuard().check(tx, {account_id}, connection) is GuardResult.OUTGOING


def test_unresolved_receiver_is_unbound(make_tx, connection, account_id) -> None:
    tx = make_tx(receiving_account_id=None, merchant_id=None)
    assert InvariantGuard().check(tx, {account_id}, connection) is GuardResult.RECEIVER_UNBOUND


def test_account_not_bound_to_connection_is_unbound(make_tx, connection, account_id) -> None:
    tx = make_tx(receiving_account_id=uuid4())
    assert InvariantGuard().check(tx, {account_id}, connection) is GuardResult.RECEIVER_UNBOUND


@pytest.mark.parametrize(
    "changes",
    [
        {"merchant_id": uuid4()},
        {"tenant_id": "tenant-b"},
        {"environment": Environment.TEST},
    ],
    ids=["other-merchant-same-tenant", "other-tenant", "other-environment"],
)
def test_scope_differing_from_connection_is_unbound(
    make_tx, connection, account_id, changes
) -> None:
    assert InvariantGuard().check(make_tx(**changes), {account_id}, connection) is (
        GuardResult.RECEIVER_UNBOUND
    )
