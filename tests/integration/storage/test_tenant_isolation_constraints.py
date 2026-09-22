"""Ownership and scope invariants enforced by composite keys (direct SQL Core).

Rows must not point across tenants, across merchants of one tenant, or across Test/Live,
even when amount, account and reference line up.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from payment_module.domain.enums import Environment, SettlementOrigin

if TYPE_CHECKING:
    from tests.integration.conftest import Seed, World

pytestmark = [pytest.mark.postgres, pytest.mark.timeout(600)]

TEST = Environment.TEST
LIVE = Environment.LIVE


# Ownership: one tenant + merchant per account per environment.


async def test_merchant_host_ref_unique_per_tenant(seed: Seed, world: World) -> None:
    row = seed.merchant_row(world.a, host_merchant_ref="shop-1")
    await seed.insert(seed.t.merchants, row)
    await seed.merchant(world.b, host_merchant_ref="shop-1")
    await seed.expect_violation(
        "merchants_tenant_host_ref_uq",
        seed.t.merchants,
        **seed.merchant_row(world.a, host_merchant_ref="shop-1"),
    )


async def test_same_account_for_other_tenant_rejected(seed: Seed, world: World) -> None:
    fingerprint = world.key[world.acc[world.m1, TEST]]
    row = seed.account_row(world.b, world.mb, TEST, account_fingerprint=fingerprint)
    await seed.expect_violation("accounts_env_fingerprint_uq", seed.t.receiving_accounts, **row)


async def test_same_account_for_other_merchant_of_same_tenant_rejected(
    seed: Seed, world: World
) -> None:
    fingerprint = world.key[world.acc[world.m1, TEST]]
    row = seed.account_row(world.a, world.m2, TEST, account_fingerprint=fingerprint)
    await seed.expect_violation("accounts_env_fingerprint_uq", seed.t.receiving_accounts, **row)


async def test_same_account_in_test_and_live_accepted(seed: Seed, world: World) -> None:
    merchant = await seed.merchant(world.a)
    await seed.account(world.a, merchant, TEST, account_fingerprint="ACB|999|")
    await seed.account(world.a, merchant, LIVE, account_fingerprint="ACB|999|")


async def test_account_of_merchant_in_other_tenant_rejected(seed: Seed, world: World) -> None:
    row = seed.account_row(world.a, world.mb, TEST)
    await seed.expect_violation("accounts_merchant_fk", seed.t.receiving_accounts, **row)


async def test_connection_of_merchant_in_other_tenant_rejected(seed: Seed, world: World) -> None:
    row = seed.connection_row(world.a, world.mb, TEST)
    await seed.expect_violation("connections_merchant_fk", seed.t.provider_connections, **row)


# Bindings.


def _binding(world: World, merchant, environment, connection, account, tenant=None):
    return {
        "tenant_id": tenant or world.a,
        "merchant_id": merchant,
        "environment": environment.value,
        "connection_id": connection,
        "receiving_account_id": account,
        "created_by": "operator-1",
    }


async def test_binding_same_merchant_and_environment_accepted(seed: Seed, world: World) -> None:
    row = _binding(world, world.m1, TEST, world.conn[world.m1, TEST], world.acc[world.m1, TEST])
    await seed.insert(seed.t.connection_account_bindings, row)


async def test_binding_account_of_other_merchant_same_tenant_rejected(
    seed: Seed, world: World
) -> None:
    row = _binding(world, world.m1, TEST, world.conn[world.m1, TEST], world.acc[world.m2, TEST])
    await seed.expect_violation("bindings_account_fk", seed.t.connection_account_bindings, **row)


async def test_binding_live_account_to_test_connection_rejected(seed: Seed, world: World) -> None:
    row = _binding(world, world.m1, TEST, world.conn[world.m1, TEST], world.acc[world.m1, LIVE])
    await seed.expect_violation("bindings_account_fk", seed.t.connection_account_bindings, **row)


async def test_binding_claiming_other_merchant_rejected(seed: Seed, world: World) -> None:
    row = _binding(world, world.m2, TEST, world.conn[world.m1, TEST], world.acc[world.m2, TEST])
    await seed.expect_violation("bindings_connection_fk", seed.t.connection_account_bindings, **row)


# Intents and supersession.


async def test_intent_with_account_of_other_tenant_rejected(seed: Seed, world: World) -> None:
    row = seed.intent_row(world.a, world.m1, TEST, world.acc[world.mb, TEST])
    await seed.expect_violation("intents_account_fk", seed.t.payment_intents, **row)


async def test_intent_with_account_of_other_merchant_same_tenant_rejected(
    seed: Seed, world: World
) -> None:
    row = seed.intent_row(world.a, world.m1, TEST, world.acc[world.m2, TEST])
    await seed.expect_violation("intents_account_fk", seed.t.payment_intents, **row)


async def test_test_intent_with_live_account_rejected(seed: Seed, world: World) -> None:
    row = seed.intent_row(world.a, world.m1, TEST, world.acc[world.m1, LIVE])
    await seed.expect_violation("intents_account_fk", seed.t.payment_intents, **row)


async def test_intent_for_merchant_of_other_tenant_rejected(seed: Seed, world: World) -> None:
    row = seed.intent_row(world.b, world.m1, TEST, world.acc[world.m1, TEST])
    await seed.expect_violation("intents_merchant_fk", seed.t.payment_intents, **row)


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        ("same", None),
        ("other_merchant", "intents_superseded_by_fk"),
        ("other_environment", "intents_superseded_by_fk"),
        ("other_tenant", "intents_superseded_by_fk"),
    ],
)
async def test_supersession_scope(
    seed: Seed, world: World, scope: str, expected: str | None
) -> None:
    targets = {
        "same": (world.a, world.m1, TEST),
        "other_merchant": (world.a, world.m2, TEST),
        "other_environment": (world.a, world.m1, LIVE),
        "other_tenant": (world.b, world.mb, TEST),
    }
    tenant, merchant, environment = targets[scope]
    replacement = await seed.intent(tenant, merchant, environment, world.acc[merchant, environment])
    row = seed.intent_row(
        world.a,
        world.m1,
        TEST,
        world.acc[world.m1, TEST],
        superseded_by_intent_id=replacement,
    )
    if expected is None:
        await seed.insert(seed.t.payment_intents, row)
    else:
        await seed.expect_violation(expected, seed.t.payment_intents, **row)


# Readiness.


async def test_readiness_environment_other_than_connection_rejected(
    seed: Seed, world: World
) -> None:
    row = seed.readiness_row(world.a, world.conn[world.m1, TEST], LIVE, 1)
    await seed.expect_violation(
        "readiness_connection_fk", seed.t.connection_reference_readiness, **row
    )


async def test_readiness_for_connection_of_other_tenant_rejected(seed: Seed, world: World) -> None:
    row = seed.readiness_row(world.a, world.conn[world.mb, TEST], TEST, 1)
    await seed.expect_violation(
        "readiness_connection_fk", seed.t.connection_reference_readiness, **row
    )


async def test_duplicate_readiness_rejected(seed: Seed, world: World) -> None:
    connection = world.conn[world.m1, TEST]
    await seed.insert(
        seed.t.connection_reference_readiness, seed.readiness_row(world.a, connection, TEST, 1)
    )
    row = seed.readiness_row(world.a, connection, TEST, 1)
    await seed.expect_violation("readiness_key_uq", seed.t.connection_reference_readiness, **row)


# Inbox.


async def test_duplicate_event_key_rejected(seed: Seed, world: World) -> None:
    connection = world.conn[world.m1, TEST]
    await seed.inbox(world.a, connection, event_key="webhook:1")
    row = seed.inbox_row(world.a, connection, event_key="webhook:1")
    await seed.expect_violation("inbox_event_key_uq", seed.t.webhook_inbox, **row)


async def test_inbox_tenant_other_than_connection_rejected(seed: Seed, world: World) -> None:
    row = seed.inbox_row(world.b, world.conn[world.m1, TEST])
    await seed.expect_violation("inbox_connection_fk", seed.t.webhook_inbox, **row)


async def test_inbox_without_body_hash_rejected(seed: Seed, world: World) -> None:
    row = seed.inbox_row(world.a, world.conn[world.m1, TEST], body_sha256=None)
    await seed.expect_not_null("body_sha256", seed.t.webhook_inbox, **row)


# Observations.


async def test_observation_of_webhook_accepted(seed: Seed, world: World) -> None:
    connection = world.conn[world.m1, TEST]
    inbox_id = await seed.inbox(world.a, connection)
    row = seed.observation_row(world.a, TEST, connection, inbox_id=inbox_id)
    await seed.insert(seed.t.provider_observations, row)


async def test_observation_environment_other_than_connection_rejected(
    seed: Seed, world: World
) -> None:
    connection = world.conn[world.m1, TEST]
    inbox_id = await seed.inbox(world.a, connection)
    row = seed.observation_row(world.a, LIVE, connection, inbox_id=inbox_id)
    await seed.expect_violation("observations_connection_fk", seed.t.provider_observations, **row)


async def test_observation_of_connection_in_other_tenant_rejected(seed: Seed, world: World) -> None:
    connection = world.conn[world.mb, TEST]
    inbox_id = await seed.inbox(world.b, connection)
    row = seed.observation_row(world.a, TEST, connection, inbox_id=inbox_id)
    await seed.expect_violation("observations_connection_fk", seed.t.provider_observations, **row)


async def test_observation_with_inbox_of_other_connection_same_tenant_rejected(
    seed: Seed, world: World
) -> None:
    other_inbox = await seed.inbox(world.a, world.conn[world.m2, TEST])
    row = seed.observation_row(world.a, TEST, world.conn[world.m1, TEST], inbox_id=other_inbox)
    await seed.expect_violation("observations_inbox_fk", seed.t.provider_observations, **row)


async def test_observation_with_inbox_of_other_tenant_rejected(seed: Seed, world: World) -> None:
    other_inbox = await seed.inbox(world.b, world.conn[world.mb, TEST])
    row = seed.observation_row(world.a, TEST, world.conn[world.m1, TEST], inbox_id=other_inbox)
    await seed.expect_violation("observations_inbox_fk", seed.t.provider_observations, **row)


async def test_observation_with_run_of_other_connection_same_tenant_rejected(
    seed: Seed, world: World
) -> None:
    other_run = await seed.run(world.a, world.conn[world.m2, TEST])
    row = seed.observation_row(world.a, TEST, world.conn[world.m1, TEST], run_id=other_run)
    await seed.expect_violation("observations_recon_run_fk", seed.t.provider_observations, **row)


async def test_observation_with_run_of_other_tenant_rejected(seed: Seed, world: World) -> None:
    other_run = await seed.run(world.b, world.conn[world.mb, TEST])
    row = seed.observation_row(world.a, TEST, world.conn[world.m1, TEST], run_id=other_run)
    await seed.expect_violation("observations_recon_run_fk", seed.t.provider_observations, **row)


async def test_webhook_observation_without_inbox_rejected(seed: Seed, world: World) -> None:
    row = seed.observation_row(world.a, TEST, world.conn[world.m1, TEST])
    await seed.expect_violation("observations_provenance_ck", seed.t.provider_observations, **row)


async def test_duplicate_observation_rejected(seed: Seed, world: World) -> None:
    connection = world.conn[world.m1, TEST]
    inbox_id = await seed.inbox(world.a, connection)
    row = seed.observation_row(world.a, TEST, connection, inbox_id=inbox_id)
    await seed.insert(seed.t.provider_observations, row)
    again = seed.observation_row(
        world.a, TEST, connection, inbox_id=inbox_id, source_tx_id=row["source_tx_id"]
    )
    await seed.expect_violation("observations_source_uq", seed.t.provider_observations, **again)


@pytest.mark.parametrize(
    ("fact_scope", "expected"),
    [
        ("same", None),
        ("live_fact", "observations_transaction_fk"),
        ("other_tenant", "observations_transaction_fk"),
        ("other_account", "observations_transaction_fk"),
    ],
)
async def test_observation_link_scope(
    seed: Seed, world: World, fact_scope: str, expected: str | None
) -> None:
    connection = world.conn[world.m1, TEST]
    key = world.key[world.acc[world.m1, TEST]]
    tenant, environment, fact_key = {
        "same": (world.a, TEST, key),
        "live_fact": (world.a, LIVE, key),
        "other_tenant": (world.b, TEST, key),
        "other_account": (world.a, TEST, "VCB|42|"),
    }[fact_scope]
    tx_id = await seed.transaction(tenant, environment, account_key=fact_key)
    inbox_id = await seed.inbox(world.a, connection)
    row = seed.observation_row(
        world.a, TEST, connection, inbox_id=inbox_id, account_key=key, transaction_id=tx_id
    )
    if expected is None:
        await seed.insert(seed.t.provider_observations, row)
    else:
        await seed.expect_violation(expected, seed.t.provider_observations, **row)


# Canonical facts.


async def test_same_webhook_id_twice_in_scope_rejected(seed: Seed, world: World) -> None:
    row = seed.transaction_row(world.a, TEST)
    await seed.insert(seed.t.provider_transactions, row)
    again = seed.transaction_row(world.a, TEST, webhook_tx_id=row["webhook_tx_id"])
    await seed.expect_violation("transactions_webhook_tx_uq", seed.t.provider_transactions, **again)


async def test_same_api_id_twice_in_scope_rejected(seed: Seed, world: World) -> None:
    await seed.transaction(world.a, TEST, webhook_tx_id=None, api_tx_id="uuid-1")
    again = seed.transaction_row(world.a, TEST, webhook_tx_id=None, api_tx_id="uuid-1")
    await seed.expect_violation("transactions_api_tx_uq", seed.t.provider_transactions, **again)


async def test_same_dedup_key_twice_in_scope_rejected(seed: Seed, world: World) -> None:
    row = seed.transaction_row(world.a, TEST)
    await seed.insert(seed.t.provider_transactions, row)
    again = seed.transaction_row(world.a, TEST, dedup_key=row["dedup_key"])
    await seed.expect_violation("transactions_dedup_uq", seed.t.provider_transactions, **again)


async def test_same_webhook_id_and_account_in_test_and_live_accepted(
    seed: Seed, world: World
) -> None:
    row = seed.transaction_row(world.a, TEST)
    await seed.insert(seed.t.provider_transactions, row)
    live = {"id": seed.transaction_row(world.a, LIVE)["id"], "environment": LIVE.value}
    await seed.insert(seed.t.provider_transactions, row | live)


async def test_same_dedup_key_in_two_tenants_accepted(seed: Seed, world: World) -> None:
    row = seed.transaction_row(world.a, TEST)
    await seed.insert(seed.t.provider_transactions, row)
    other = {"id": seed.transaction_row(world.b, TEST)["id"], "tenant_id": world.b}
    await seed.insert(seed.t.provider_transactions, row | other)


async def test_same_bank_reference_twice_accepted(seed: Seed, world: World) -> None:
    await seed.transaction(world.a, TEST, bank_reference="FT123")
    await seed.transaction(world.a, TEST, bank_reference="FT123")


@pytest.mark.parametrize("missing", ["merchant_id", "receiving_account_id"])
async def test_fact_with_half_resolved_receiver_rejected(
    seed: Seed, world: World, missing: str
) -> None:
    account = world.acc[world.m1, TEST]
    row = seed.transaction_row(world.a, TEST, merchant_id=world.m1, account_id=account)
    row[missing] = None
    await seed.expect_violation(
        "transactions_receiver_pair_ck", seed.t.provider_transactions, **row
    )


async def test_fact_resolved_to_account_of_other_merchant_rejected(
    seed: Seed, world: World
) -> None:
    row = seed.transaction_row(
        world.a, TEST, merchant_id=world.m1, account_id=world.acc[world.m2, TEST]
    )
    await seed.expect_violation("transactions_account_fk", seed.t.provider_transactions, **row)


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        ("same", None),
        ("other_environment", "transactions_duplicate_of_fk"),
        ("other_tenant", "transactions_duplicate_of_fk"),
    ],
)
async def test_duplicate_of_scope(
    seed: Seed, world: World, scope: str, expected: str | None
) -> None:
    tenant, environment = {
        "same": (world.a, TEST),
        "other_environment": (world.a, LIVE),
        "other_tenant": (world.b, TEST),
    }[scope]
    original = await seed.transaction(tenant, environment)
    row = seed.transaction_row(world.a, TEST, duplicate_of_transaction_id=original)
    if expected is None:
        await seed.insert(seed.t.provider_transactions, row)
    else:
        await seed.expect_violation(expected, seed.t.provider_transactions, **row)


# Review cases.


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        ("same", None),
        ("other_environment", "review_cases_candidate_fk"),
        ("other_tenant", "review_cases_candidate_fk"),
    ],
)
async def test_review_candidate_scope(
    seed: Seed, world: World, scope: str, expected: str | None
) -> None:
    tenant, merchant, environment = {
        "same": (world.a, world.m1, TEST),
        "other_environment": (world.a, world.m1, LIVE),
        "other_tenant": (world.b, world.mb, TEST),
    }[scope]
    candidate = await seed.intent(tenant, merchant, environment, world.acc[merchant, environment])
    tx_id = await seed.transaction(world.a, TEST)
    row = seed.review_row(world.a, TEST, tx_id, candidate_intent_id=candidate)
    if expected is None:
        await seed.insert(seed.t.review_cases, row)
    else:
        await seed.expect_violation(expected, seed.t.review_cases, **row)


async def test_review_environment_other_than_fact_rejected(seed: Seed, world: World) -> None:
    tx_id = await seed.transaction(world.a, TEST)
    row = seed.review_row(world.a, LIVE, tx_id)
    await seed.expect_violation("review_cases_transaction_fk", seed.t.review_cases, **row)


async def test_review_of_fact_in_other_tenant_rejected(seed: Seed, world: World) -> None:
    tx_id = await seed.transaction(world.b, TEST)
    row = seed.review_row(world.a, TEST, tx_id)
    await seed.expect_violation("review_cases_transaction_fk", seed.t.review_cases, **row)


# Settlements across scopes and operator provenance.


async def _bound_fact(seed: Seed, world: World, environment: Environment):
    account = world.acc[world.m1, environment]
    return await seed.transaction(
        world.a,
        environment,
        account_key=world.key[account],
        merchant_id=world.m1,
        account_id=account,
    )


async def test_test_fact_settling_live_intent_rejected(seed: Seed, world: World) -> None:
    # Same tenant, merchant, amount and account fingerprint; only the environment differs.
    live_intent = await seed.intent(world.a, world.m1, LIVE, world.acc[world.m1, LIVE])
    test_fact = await _bound_fact(seed, world, TEST)
    for environment, account in (
        (TEST, world.acc[world.m1, TEST]),
        (LIVE, world.acc[world.m1, LIVE]),
    ):
        row = seed.settlement_row(
            world.a,
            environment,
            transaction_id=test_fact,
            intent_id=live_intent,
            account_id=account,
        )
        expected = "settlements_intent_fk" if environment == TEST else "settlements_transaction_fk"
        await seed.expect_violation(expected, seed.t.settlements, **row)


async def test_settlement_across_tenants_rejected(seed: Seed, world: World) -> None:
    other_intent = await seed.intent(world.b, world.mb, TEST, world.acc[world.mb, TEST])
    fact = await _bound_fact(seed, world, TEST)
    row = seed.settlement_row(
        world.a,
        TEST,
        transaction_id=fact,
        intent_id=other_intent,
        account_id=world.acc[world.m1, TEST],
    )
    await seed.expect_violation("settlements_intent_fk", seed.t.settlements, **row)


async def test_operator_settlement_without_review_case_rejected(seed: Seed, world: World) -> None:
    account = world.acc[world.m1, TEST]
    intent_id = await seed.intent(world.a, world.m1, TEST, account)
    fact = await _bound_fact(seed, world, TEST)
    row = seed.settlement_row(
        world.a,
        TEST,
        transaction_id=fact,
        intent_id=intent_id,
        account_id=account,
        origin=SettlementOrigin.OPERATOR_REVIEW.value,
        resolved_by="operator-1",
    )
    await seed.expect_violation("settlements_operator_provenance_ck", seed.t.settlements, **row)


async def test_operator_settlement_without_actor_rejected(seed: Seed, world: World) -> None:
    account = world.acc[world.m1, TEST]
    intent_id = await seed.intent(world.a, world.m1, TEST, account)
    fact = await _bound_fact(seed, world, TEST)
    case_id = await seed.review(world.a, TEST, fact)
    row = seed.settlement_row(
        world.a,
        TEST,
        transaction_id=fact,
        intent_id=intent_id,
        account_id=account,
        origin=SettlementOrigin.OPERATOR_REVIEW.value,
        review_case_id=case_id,
    )
    await seed.expect_violation("settlements_operator_provenance_ck", seed.t.settlements, **row)


async def test_operator_settlement_with_case_of_other_fact_rejected(
    seed: Seed, world: World
) -> None:
    account = world.acc[world.m1, TEST]
    intent_id = await seed.intent(world.a, world.m1, TEST, account)
    fact = await _bound_fact(seed, world, TEST)
    other_case = await seed.review(world.a, TEST, await seed.transaction(world.a, TEST))
    row = seed.settlement_row(
        world.a,
        TEST,
        transaction_id=fact,
        intent_id=intent_id,
        account_id=account,
        origin=SettlementOrigin.OPERATOR_REVIEW.value,
        review_case_id=other_case,
        resolved_by="operator-1",
    )
    await seed.expect_violation("settlements_review_case_fk", seed.t.settlements, **row)


async def test_operator_settlement_with_its_review_case_accepted(seed: Seed, world: World) -> None:
    account = world.acc[world.m1, TEST]
    intent_id = await seed.intent(world.a, world.m1, TEST, account)
    fact = await _bound_fact(seed, world, TEST)
    case_id = await seed.review(world.a, TEST, fact, candidate_intent_id=intent_id)
    row = seed.settlement_row(
        world.a,
        TEST,
        transaction_id=fact,
        intent_id=intent_id,
        account_id=account,
        origin=SettlementOrigin.OPERATOR_REVIEW.value,
        review_case_id=case_id,
        resolved_by="operator-1",
    )
    await seed.insert(seed.t.settlements, row)
