"""Money, state and profile invariants enforced by PostgreSQL itself (direct SQL Core)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from payment_module.domain.enums import (
    ConnectionStatus,
    Direction,
    Environment,
    MatchState,
    ProfileKind,
    ProfileStatus,
    ReconcileMode,
    ReviewCaseStatus,
    SettlementOrigin,
)

if TYPE_CHECKING:
    from tests.integration.conftest import Seed, World

pytestmark = [pytest.mark.postgres, pytest.mark.timeout(600)]

TEST = Environment.TEST
LIVE = Environment.LIVE


async def _settleable(seed: Seed, world: World, *, amount: int = 150_000, fact_amount=None):
    """An intent and an incoming fact on the same account of merchant m1 (Test)."""
    account = world.acc[world.m1, TEST]
    intent_id = await seed.intent(world.a, world.m1, TEST, account, amount=amount)
    tx_id = await seed.transaction(
        world.a,
        TEST,
        account_key=world.key[account],
        merchant_id=world.m1,
        account_id=account,
        amount=amount if fact_amount is None else fact_amount,
    )
    return account, intent_id, tx_id


async def test_exact_amount_settlement_accepted(seed: Seed, world: World) -> None:
    account, intent_id, tx_id = await _settleable(seed, world)
    await seed.insert(
        seed.t.settlements,
        seed.settlement_row(
            world.a, TEST, transaction_id=tx_id, intent_id=intent_id, account_id=account
        ),
    )


async def test_auto_amount_mismatch_rejected(seed: Seed, world: World) -> None:
    account, intent_id, tx_id = await _settleable(seed, world, fact_amount=140_000)
    row = seed.settlement_row(
        world.a,
        TEST,
        transaction_id=tx_id,
        intent_id=intent_id,
        account_id=account,
        amount=140_000,
    )
    await seed.expect_violation("settlements_exact_amount_ck", seed.t.settlements, **row)


async def test_operator_review_amount_mismatch_rejected(seed: Seed, world: World) -> None:
    account, intent_id, tx_id = await _settleable(seed, world, fact_amount=140_000)
    case_id = await seed.review(world.a, TEST, tx_id, candidate_intent_id=intent_id)
    row = seed.settlement_row(
        world.a,
        TEST,
        transaction_id=tx_id,
        intent_id=intent_id,
        account_id=account,
        amount=140_000,
        origin=SettlementOrigin.OPERATOR_REVIEW.value,
        review_case_id=case_id,
        resolved_by="operator-1",
    )
    await seed.expect_violation("settlements_exact_amount_ck", seed.t.settlements, **row)


async def test_settlement_with_both_amounts_equal_but_fact_amount_different_rejected(
    seed: Seed, world: World
) -> None:
    # Writing the intent amount in both columns must not hide a fact of another amount.
    account, intent_id, tx_id = await _settleable(seed, world, fact_amount=140_000)
    row = seed.settlement_row(
        world.a, TEST, transaction_id=tx_id, intent_id=intent_id, account_id=account
    )
    await seed.expect_violation("settlements_transaction_fk", seed.t.settlements, **row)


async def test_settlement_null_amount_rejected(seed: Seed, world: World) -> None:
    account, intent_id, tx_id = await _settleable(seed, world)
    row = seed.settlement_row(
        world.a, TEST, transaction_id=tx_id, intent_id=intent_id, account_id=account
    )
    await seed.expect_not_null("amount_vnd", seed.t.settlements, **(row | {"amount_vnd": None}))


async def test_settlement_receiver_other_than_intent_rejected(seed: Seed, world: World) -> None:
    account = world.acc[world.m1, TEST]
    other = await seed.account(world.a, world.m1, TEST)
    intent_id = await seed.intent(world.a, world.m1, TEST, account)
    tx_id = await seed.transaction(
        world.a, TEST, account_key=world.key[account], merchant_id=world.m1, account_id=other
    )
    row = seed.settlement_row(
        world.a, TEST, transaction_id=tx_id, intent_id=intent_id, account_id=other
    )
    await seed.expect_violation("settlements_intent_fk", seed.t.settlements, **row)


async def test_settlement_for_unbound_fact_rejected(seed: Seed, world: World) -> None:
    account = world.acc[world.m1, TEST]
    intent_id = await seed.intent(world.a, world.m1, TEST, account)
    tx_id = await seed.transaction(world.a, TEST, account_key=world.key[account])
    row = seed.settlement_row(
        world.a, TEST, transaction_id=tx_id, intent_id=intent_id, account_id=account
    )
    await seed.expect_violation("settlements_transaction_fk", seed.t.settlements, **row)


async def test_two_settlements_for_one_intent_rejected(seed: Seed, world: World) -> None:
    account, intent_id, tx_id = await _settleable(seed, world)
    await seed.insert(
        seed.t.settlements,
        seed.settlement_row(
            world.a, TEST, transaction_id=tx_id, intent_id=intent_id, account_id=account
        ),
    )
    second_tx = await seed.transaction(
        world.a, TEST, account_key=world.key[account], merchant_id=world.m1, account_id=account
    )
    row = seed.settlement_row(
        world.a, TEST, transaction_id=second_tx, intent_id=intent_id, account_id=account
    )
    await seed.expect_violation("settlements_intent_uq", seed.t.settlements, **row)


async def test_two_settlements_for_one_fact_rejected(seed: Seed, world: World) -> None:
    account, intent_id, tx_id = await _settleable(seed, world)
    await seed.insert(
        seed.t.settlements,
        seed.settlement_row(
            world.a, TEST, transaction_id=tx_id, intent_id=intent_id, account_id=account
        ),
    )
    second_intent = await seed.intent(world.a, world.m1, TEST, account)
    row = seed.settlement_row(
        world.a, TEST, transaction_id=tx_id, intent_id=second_intent, account_id=account
    )
    await seed.expect_violation("settlements_transaction_uq", seed.t.settlements, **row)


async def test_settled_outgoing_fact_rejected(seed: Seed, world: World) -> None:
    account = world.acc[world.m1, TEST]
    row = seed.transaction_row(
        world.a,
        TEST,
        account_key=world.key[account],
        merchant_id=world.m1,
        account_id=account,
        direction=Direction.OUT,
        match_state=MatchState.SETTLED.value,
    )
    await seed.expect_violation(
        "transactions_settled_shape_ck", seed.t.provider_transactions, **row
    )


async def test_settled_unbound_fact_rejected(seed: Seed, world: World) -> None:
    row = seed.transaction_row(world.a, TEST, match_state=MatchState.SETTLED.value)
    await seed.expect_violation(
        "transactions_settled_shape_ck", seed.t.provider_transactions, **row
    )


async def test_negative_fact_amount_rejected(seed: Seed, world: World) -> None:
    row = seed.transaction_row(world.a, TEST, amount=-1)
    await seed.expect_violation("transactions_amount_ck", seed.t.provider_transactions, **row)


async def test_zero_intent_amount_rejected(seed: Seed, world: World) -> None:
    row = seed.intent_row(world.a, world.m1, TEST, world.acc[world.m1, TEST], amount=0)
    await seed.expect_violation("intents_amount_positive_ck", seed.t.payment_intents, **row)


async def test_two_open_reviews_for_one_fact_rejected(seed: Seed, world: World) -> None:
    tx_id = await seed.transaction(world.a, TEST)
    await seed.review(world.a, TEST, tx_id)
    row = seed.review_row(world.a, TEST, tx_id)
    await seed.expect_violation("review_cases_one_open_uq", seed.t.review_cases, **row)


async def test_resolved_review_does_not_block_a_new_open_one(seed: Seed, world: World) -> None:
    tx_id = await seed.transaction(world.a, TEST)
    await seed.review(world.a, TEST, tx_id, status=ReviewCaseStatus.RESOLVED.value)
    await seed.review(world.a, TEST, tx_id)


async def test_auto_settle_without_evidence_rejected(seed: Seed, world: World) -> None:
    row = seed.connection_row(
        world.a, world.m1, TEST, reconcile_mode=ReconcileMode.AUTO_SETTLE.value
    )
    await seed.expect_violation(
        "connections_auto_settle_evidence_ck", seed.t.provider_connections, **row
    )


async def test_auto_settle_with_evidence_accepted(seed: Seed, world: World) -> None:
    await seed.connection(
        world.a,
        world.m1,
        TEST,
        reconcile_mode=ReconcileMode.AUTO_SETTLE.value,
        reconcile_evidence_ref="evidence:sepay-test-a",
    )


@pytest.mark.parametrize("tolerance", [30, 7201])
async def test_timestamp_tolerance_out_of_range_rejected(
    seed: Seed, world: World, tolerance: int
) -> None:
    row = seed.connection_row(world.a, world.m1, TEST, timestamp_tolerance_seconds=tolerance)
    await seed.expect_violation("connections_tolerance_ck", seed.t.provider_connections, **row)


async def test_connection_defaults_are_safe(seed: Seed, world: World) -> None:
    row = seed.connection_row(world.a, world.m1, TEST)
    del row["status"]
    await seed.insert(seed.t.provider_connections, row)
    table = seed.t.provider_connections
    stored = (
        await seed.conn.execute(
            table.select()
            .with_only_columns(
                table.c.reconcile_mode, table.c.timestamp_tolerance_seconds, table.c.status
            )
            .where(table.c.id == row["id"])
        )
    ).one()
    assert tuple(stored) == (
        ReconcileMode.DETECT_ONLY.value,
        300,
        ConnectionStatus.PENDING.value,
    )


async def test_two_active_profiles_rejected(seed: Seed, world: World) -> None:
    await seed.expect_violation(
        "profiles_one_active_uq",
        seed.t.reference_profiles,
        version=2,
        kind=ProfileKind.GENERATED.value,
        suffix_length=6,
        alphabet="0123456789",
        status=ProfileStatus.ACTIVE.value,
    )


async def test_generated_profile_without_shape_rejected(seed: Seed, world: World) -> None:
    await seed.expect_violation(
        "profiles_generated_shape_ck",
        seed.t.reference_profiles,
        version=2,
        kind=ProfileKind.GENERATED.value,
        status=ProfileStatus.DRAFT.value,
    )


async def test_legacy_profile_without_shape_accepted(seed: Seed, world: World) -> None:
    await seed.profile(
        2,
        ProfileStatus.ACCEPTED_LEGACY,
        kind=ProfileKind.LEGACY_IMPORT.value,
        suffix_length=None,
        alphabet=None,
    )


async def test_duplicate_prefix_in_version_rejected(seed: Seed, world: World) -> None:
    await seed.expect_violation(
        "prefixes_version_prefix_uq",
        seed.t.reference_profile_prefixes,
        profile_version=1,
        name="topup",
        prefix="SUB",
    )


async def test_same_prefix_in_two_versions_accepted(seed: Seed, world: World) -> None:
    await seed.profile(2)
    await seed.prefix(2, "subscription", "SUB")


@pytest.mark.parametrize("prefix", ["S", "SUBSCR"])
async def test_prefix_length_rejected(seed: Seed, world: World, prefix: str) -> None:
    await seed.expect_violation(
        "prefixes_prefix_len_ck",
        seed.t.reference_profile_prefixes,
        profile_version=1,
        name="topup",
        prefix=prefix,
    )


async def test_prefix_name_too_long_rejected(seed: Seed, world: World) -> None:
    await seed.expect_violation(
        "prefixes_name_len_ck",
        seed.t.reference_profile_prefixes,
        profile_version=1,
        name="n" * 33,
        prefix="TOP",
    )


async def test_intent_with_unknown_prefix_name_rejected(seed: Seed, world: World) -> None:
    row = seed.intent_row(world.a, world.m1, TEST, world.acc[world.m1, TEST], prefix_name="topup")
    await seed.expect_violation("intents_prefix_fk", seed.t.payment_intents, **row)


async def test_intent_with_unknown_profile_version_rejected(seed: Seed, world: World) -> None:
    row = seed.intent_row(
        world.a, world.m1, TEST, world.acc[world.m1, TEST], version=9, prefix_name=None
    )
    await seed.expect_violation("intents_profile_fk", seed.t.payment_intents, **row)


async def test_duplicate_payment_reference_rejected(seed: Seed, world: World) -> None:
    first = seed.intent_row(world.a, world.m1, TEST, world.acc[world.m1, TEST])
    await seed.insert(seed.t.payment_intents, first)
    row = seed.intent_row(
        world.b,
        world.mb,
        LIVE,
        world.acc[world.mb, LIVE],
        payment_reference=first["payment_reference"],
    )
    await seed.expect_violation("intents_reference_uq", seed.t.payment_intents, **row)


async def test_duplicate_idempotency_key_rejected(seed: Seed, world: World) -> None:
    first = seed.intent_row(world.a, world.m1, TEST, world.acc[world.m1, TEST])
    await seed.insert(seed.t.payment_intents, first)
    row = seed.intent_row(
        world.a,
        world.m2,
        TEST,
        world.acc[world.m2, TEST],
        idempotency_key=first["idempotency_key"],
    )
    await seed.expect_violation("intents_idempotency_uq", seed.t.payment_intents, **row)


async def test_same_idempotency_key_in_other_environment_accepted(seed: Seed, world: World) -> None:
    first = seed.intent_row(world.a, world.m1, TEST, world.acc[world.m1, TEST])
    await seed.insert(seed.t.payment_intents, first)
    await seed.intent(
        world.a, world.m1, LIVE, world.acc[world.m1, LIVE], idempotency_key=first["idempotency_key"]
    )
