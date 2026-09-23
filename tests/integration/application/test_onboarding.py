"""Onboarding: merchants, accounts with one owner per environment, connections, bindings in
one merchant scope, and status changes that never keep a connection active on stale
readiness."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from fakes.fake_checklist import SHARED_ITEMS, FakeChecklist
from fakes.payment_app import TENANT_B, App, open_case
from payment_module.builder import PaymentModule
from payment_module.domain.account_identity import account_key
from payment_module.domain.enums import (
    ConnectionStatus,
    Environment,
    MatchState,
    ReceivingAccountStatus,
    ReconcileMode,
    ReviewReason,
)
from payment_module.domain.errors import (
    IllegalTransition,
    IntentRejected,
    OnboardingRejected,
    ReadinessChecklistIncomplete,
)

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

ACTOR = "ops@example.test"
V1_ITEMS = {key: True for key in SHARED_ITEMS} | {
    f"{kind}:{prefix}": True
    for kind in ("template", "webhook_filter")
    for prefix in ("SUB", "TOP", "INV")
}


@pytest.fixture
def ops(app: App) -> PaymentModule:
    return app.build(template_checklists={"fake": FakeChecklist()})


async def connection_row(app: App, connection_id) -> sa.Row:
    t = app.tables.provider_connections
    [row] = await app.rows(t, t.c.id == connection_id)
    return row


async def test_register_merchant_is_idempotent_on_the_host_reference(app: App) -> None:
    first = await app.module.register_merchant.execute("tenant-c", "shop-1", ACTOR)
    again = await app.module.register_merchant.execute("tenant-c", "shop-1", ACTOR)
    other_tenant = await app.module.register_merchant.execute("tenant-d", "shop-1", ACTOR)

    assert first.id == again.id
    assert other_tenant.id != first.id


async def test_register_account_uses_the_shared_account_key(app: App) -> None:
    account = await app.module.register_receiving_account.execute(
        app.m1.tenant_id,
        app.m1.merchant_id,
        Environment.TEST,
        " vcb ",
        " 0123 456789 ",
        "CONG TY A",
        ACTOR,
        sub_account="va01",
    )

    t = app.tables.receiving_accounts
    [row] = await app.rows(t, t.c.id == account.id)
    assert row.account_fingerprint == account_key("VCB", "0123456789", "VA01")
    assert row.account_fingerprint == "VCB|0123456789|VA01"
    assert row.account_number_masked == "****6789"
    assert row.status == ReceivingAccountStatus.ACTIVE.value


async def test_one_owner_per_account_and_environment(app: App) -> None:
    number = app.m1.account_number
    for tenant_id, merchant_id in [
        (app.m1.tenant_id, app.m2.merchant_id),
        (TENANT_B, app.mb.merchant_id),
    ]:
        with pytest.raises(OnboardingRejected) as caught:
            await app.module.register_receiving_account.execute(
                tenant_id, merchant_id, Environment.TEST, "VCB", number, "X", ACTOR
            )
        assert caught.value.code == "ACCOUNT_ALREADY_OWNED"

    live = await app.module.register_receiving_account.execute(
        app.mb.tenant_id, app.mb.merchant_id, Environment.LIVE, "VCB", number, "X", ACTOR
    )
    assert live.environment == Environment.LIVE


async def test_register_connection_starts_pending_and_detect_only(app: App) -> None:
    connection = await app.module.register_connection.execute(
        app.m1.tenant_id, app.m1.merchant_id, "fake", Environment.TEST, "env:SECRET", ACTOR
    )

    assert connection.status == ConnectionStatus.PENDING
    assert connection.reconcile_mode == ReconcileMode.DETECT_ONLY
    assert connection.environment == Environment.TEST
    assert len(connection.locator) >= 32
    other = await app.module.register_connection.execute(
        app.m1.tenant_id, app.m1.merchant_id, "fake", Environment.TEST, "env:SECRET", ACTOR
    )
    assert other.locator != connection.locator
    with pytest.raises(OnboardingRejected) as caught:
        await app.module.register_connection.execute(
            app.m1.tenant_id, app.m1.merchant_id, "unknown", Environment.TEST, "ref", ACTOR
        )
    assert caught.value.code == "UNKNOWN_PROVIDER"


@pytest.mark.parametrize("target", ["other_merchant", "other_environment"])
async def test_bind_across_merchant_or_environment_is_refused(app: App, target: str) -> None:
    account_id = app.m2.account_id if target == "other_merchant" else app.m1_live.account_id

    with pytest.raises(OnboardingRejected) as caught:
        await app.module.bind_connection_account.execute(
            app.m1.tenant_id, app.m1.connection_id, account_id, ACTOR
        )
    assert caught.value.code == "SCOPE_MISMATCH"
    assert (await connection_row(app, app.m1.connection_id)).status == "active"


async def test_database_refuses_a_cross_merchant_binding(app: App) -> None:
    b = app.tables.connection_account_bindings
    with pytest.raises(IntegrityError) as caught:
        await app.execute(
            b.insert().values(
                tenant_id=app.m1.tenant_id,
                merchant_id=app.m1.merchant_id,
                environment="test",
                connection_id=app.m1.connection_id,
                receiving_account_id=app.m2.account_id,
                created_by="sql",
            )
        )
    assert "bindings_account_fk" in str(caught.value)


async def test_binding_invalidates_readiness_until_recorded_again(
    app: App, ops: PaymentModule
) -> None:
    await ops.record_connection_readiness.execute(
        app.m1.tenant_id, app.m1.connection_id, 1, Environment.TEST, V1_ITEMS, "e", ACTOR
    )
    t = app.tables.provider_connections
    await app.execute(
        sa.update(t)
        .where(t.c.id == app.m1.connection_id)
        .values(reconcile_mode="auto_settle", reconcile_evidence_ref="evidence.json")
    )
    extra = await ops.register_receiving_account.execute(
        app.m1.tenant_id, app.m1.merchant_id, Environment.TEST, "VCB", "6660001", "SHOP", ACTOR
    )

    await ops.bind_connection_account.execute(
        app.m1.tenant_id, app.m1.connection_id, extra.id, ACTOR
    )

    row = await connection_row(app, app.m1.connection_id)
    assert (row.status, row.reconcile_mode, row.reconcile_evidence_ref) == (
        "not_ready",
        "detect_only",
        None,
    )
    assert (row.status_changed_by, row.status_changed_reason) == (ACTOR, "account bound")
    r = app.tables.connection_reference_readiness
    [readiness] = await app.rows(r, r.c.connection_id == app.m1.connection_id)
    assert readiness.status == "pending"
    with pytest.raises(OnboardingRejected) as caught:
        await ops.set_connection_status.execute(
            app.m1.tenant_id, app.m1.connection_id, ConnectionStatus.ACTIVE, ACTOR, "retry"
        )
    assert caught.value.code == "CONNECTION_NOT_READY"
    with pytest.raises(IntentRejected):
        await ops.create_intent.execute(app.command())

    result = await ops.record_connection_readiness.execute(
        app.m1.tenant_id, app.m1.connection_id, 1, Environment.TEST, V1_ITEMS, "e2", ACTOR
    )
    assert result.connection_status == ConnectionStatus.ACTIVE
    assert (await ops.create_intent.execute(app.command())).created


async def test_binding_the_same_account_again_changes_nothing(app: App) -> None:
    await app.module.bind_connection_account.execute(
        app.m1.tenant_id, app.m1.connection_id, app.m1.account_id, ACTOR
    )
    assert (await connection_row(app, app.m1.connection_id)).status == "active"


async def test_new_connection_becomes_active_only_when_bound_and_ready(
    app: App, ops: PaymentModule
) -> None:
    connection = await ops.register_connection.execute(
        app.m2.tenant_id, app.m2.merchant_id, "fake", Environment.TEST, "env:SECRET", ACTOR
    )
    with pytest.raises(OnboardingRejected) as caught:
        await ops.set_connection_status.execute(
            app.m2.tenant_id, connection.id, ConnectionStatus.ACTIVE, ACTOR, "go live"
        )
    assert caught.value.code == "NO_BOUND_ACCOUNT"
    await ops.bind_connection_account.execute(
        app.m2.tenant_id, connection.id, app.m2.account_id, ACTOR
    )
    with pytest.raises(OnboardingRejected) as caught:
        await ops.set_connection_status.execute(
            app.m2.tenant_id, connection.id, ConnectionStatus.ACTIVE, ACTOR, "go live"
        )
    assert caught.value.code == "CONNECTION_NOT_READY"

    result = await ops.record_connection_readiness.execute(
        app.m2.tenant_id, connection.id, 1, Environment.TEST, V1_ITEMS, "e", ACTOR
    )

    assert result.connection_status == ConnectionStatus.ACTIVE
    row = await connection_row(app, connection.id)
    assert (row.status, row.status_changed_by) == ("active", ACTOR)


async def test_checklist_cannot_claim_a_binding_that_does_not_exist(
    app: App, ops: PaymentModule
) -> None:
    """``accounts:bound`` comes from storage: a confirmed flag without a binding is not
    enough, and the connection stays out of ``active``."""
    connection = await ops.register_connection.execute(
        app.m2.tenant_id, app.m2.merchant_id, "fake", Environment.TEST, "env:SECRET", ACTOR
    )
    assert V1_ITEMS["accounts:bound"] is True

    with pytest.raises(ReadinessChecklistIncomplete) as caught:
        await ops.record_connection_readiness.execute(
            app.m2.tenant_id, connection.id, 1, Environment.TEST, V1_ITEMS, "e", ACTOR
        )

    assert caught.value.missing == ("accounts:bound",)
    assert (await connection_row(app, connection.id)).status == "pending"
    r = app.tables.connection_reference_readiness
    assert await app.count(r, r.c.connection_id == connection.id) == 0


async def test_activation_needs_an_active_bound_account(app: App, ops: PaymentModule) -> None:
    await ops.record_connection_readiness.execute(
        app.m1.tenant_id, app.m1.connection_id, 1, Environment.TEST, V1_ITEMS, "e", ACTOR
    )
    await ops.set_connection_status.execute(
        app.m1.tenant_id, app.m1.connection_id, ConnectionStatus.NOT_READY, ACTOR, "pause"
    )
    await ops.set_receiving_account_status.execute(
        app.m1.tenant_id, app.m1.account_id, ReceivingAccountStatus.DISABLED, ACTOR, "pause"
    )

    with pytest.raises(OnboardingRejected) as caught:
        await ops.set_connection_status.execute(
            app.m1.tenant_id, app.m1.connection_id, ConnectionStatus.ACTIVE, ACTOR, "resume"
        )
    assert caught.value.code == "NO_BOUND_ACCOUNT"
    assert (await connection_row(app, app.m1.connection_id)).status == "not_ready"


async def test_status_changes_are_audited_and_follow_the_lifecycle(app: App) -> None:
    with pytest.raises(OnboardingRejected) as caught:
        await app.module.set_connection_status.execute(
            app.m1.tenant_id, app.m1.connection_id, ConnectionStatus.NOT_READY, ACTOR, " "
        )
    assert caught.value.code == "REASON_REQUIRED"
    await app.module.set_connection_status.execute(
        app.m1.tenant_id, app.m1.connection_id, ConnectionStatus.DISABLED, ACTOR, "fraud check"
    )
    row = await connection_row(app, app.m1.connection_id)
    assert (row.status, row.status_changed_by, row.status_changed_reason) == (
        "disabled",
        ACTOR,
        "fraud check",
    )
    assert row.status_changed_at is not None
    with pytest.raises(IllegalTransition):
        await app.module.set_connection_status.execute(
            app.m1.tenant_id, app.m1.connection_id, ConnectionStatus.PENDING, ACTOR, "reset"
        )


async def test_disabled_account_still_settles_issued_intent(app: App) -> None:
    created = await app.intent()
    await app.module.set_receiving_account_status.execute(
        app.m1.tenant_id,
        app.m1.account_id,
        ReceivingAccountStatus.DISABLED,
        ACTOR,
        "merchant asked to pause",
    )

    await app.pay(code=created.payment_reference)

    t = app.tables.provider_transactions
    [fact] = await app.rows(t)
    assert fact.match_state == MatchState.SETTLED.value
    with pytest.raises(IntentRejected) as caught:
        await app.intent()
    assert caught.value.code == "RECEIVING_ACCOUNT_NOT_READY"


async def test_retired_account_money_goes_to_review(app: App) -> None:
    created = await app.intent()
    await app.module.set_receiving_account_status.execute(
        app.m1.tenant_id,
        app.m1.account_id,
        ReceivingAccountStatus.RETIRED,
        ACTOR,
        "account closed at the bank",
    )

    b = app.tables.connection_account_bindings
    assert await app.count(b, b.c.receiving_account_id == app.m1.account_id) == 0
    row = await connection_row(app, app.m1.connection_id)
    assert (row.status, row.reconcile_mode) == ("not_ready", "detect_only")
    await app.pay(code=created.payment_reference)
    assert (await open_case(app)).reason == ReviewReason.RECEIVER_UNBOUND.value
    assert await app.count(app.tables.settlements) == 0
    with pytest.raises(IllegalTransition):
        await app.module.set_receiving_account_status.execute(
            app.m1.tenant_id, app.m1.account_id, ReceivingAccountStatus.ACTIVE, ACTOR, "undo"
        )
