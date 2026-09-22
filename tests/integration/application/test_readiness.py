"""RecordConnectionReadiness: checklist per connection x profile version x environment."""

from __future__ import annotations

import pytest

from fakes.fake_checklist import SHARED_ITEMS, FakeChecklist
from fakes.payment_app import App
from payment_module.builder import PaymentModule
from payment_module.domain.enums import ConnectionStatus, Environment
from payment_module.domain.errors import (
    OnboardingRejected,
    ReadinessChecklistIncomplete,
    ReferenceProfileRejected,
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


async def record(module: PaymentModule, app: App, items=V1_ITEMS, **over):
    values = {
        "tenant_id": app.m1.tenant_id,
        "connection_id": app.m1.connection_id,
        "profile_version": 1,
        "environment": Environment.TEST,
        "checklist": items,
        "evidence_ref": "ticket-42",
        "verified_by": ACTOR,
    } | over
    return await module.record_connection_readiness.execute(**values)


async def test_ready_is_stored_per_connection_version_and_environment(
    app: App, ops: PaymentModule
) -> None:
    result = await record(ops, app)
    again = await record(ops, app, evidence_ref="ticket-43")

    assert again.readiness.id == result.readiness.id
    r = app.tables.connection_reference_readiness
    [row] = await app.rows(r)
    assert (row.status, row.profile_version, row.environment) == ("ready", 1, "test")
    assert (row.evidence_ref, row.verified_by) == ("ticket-43", ACTOR)
    assert row.verified_at is not None
    assert set(row.checklist["confirmed"]) == set(V1_ITEMS)


async def test_every_shared_item_is_required(app: App, ops: PaymentModule) -> None:
    for key in SHARED_ITEMS:
        if key == "accounts:bound":
            continue
        items = {k: v for k, v in V1_ITEMS.items() if k != key}
        with pytest.raises(ReadinessChecklistIncomplete) as caught:
            await record(ops, app, items)
        assert caught.value.missing == (key,)


@pytest.mark.parametrize("flag", [None, False, True])
async def test_bound_accounts_come_from_storage_not_the_checklist(
    app: App, ops: PaymentModule, flag: bool | None
) -> None:
    """m1's connection has an active bound account, so ``accounts:bound`` holds whatever
    the operator ticked; the stored confirmation records the derived value."""
    items = {k: v for k, v in V1_ITEMS.items() if k != "accounts:bound"}
    if flag is not None:
        items["accounts:bound"] = flag

    result = await record(ops, app, items)

    assert result.readiness.status == "ready"
    assert "accounts:bound" in result.readiness.confirmed


async def test_bound_account_item_is_required_even_if_the_provider_omits_it(app: App) -> None:
    class NoBindingItem(FakeChecklist):
        def checklist(self, connection, profile, accepted=()):
            full = super().checklist(connection, profile, accepted)
            return type(full)(tuple(i for i in full.items if i.key != "accounts:bound"))

    module = app.build(template_checklists={"fake": NoBindingItem()})
    connection = await module.register_connection.execute(
        app.m2.tenant_id, app.m2.merchant_id, "fake", Environment.TEST, "env:SECRET", ACTOR
    )

    with pytest.raises(ReadinessChecklistIncomplete) as caught:
        await record(module, app, V1_ITEMS, tenant_id=app.m2.tenant_id, connection_id=connection.id)
    assert caught.value.missing == ("accounts:bound",)


async def test_prefix_items_are_required_even_if_the_provider_omits_them(app: App) -> None:
    class SharedOnly(FakeChecklist):
        def checklist(self, connection, profile, accepted=()):
            full = super().checklist(connection, profile, accepted)
            return type(full)(tuple(i for i in full.items if i.key in SHARED_ITEMS))

    module = app.build(template_checklists={"fake": SharedOnly()})
    items = {key: True for key in SHARED_ITEMS}

    with pytest.raises(ReadinessChecklistIncomplete) as caught:
        await record(module, app, items)
    assert set(caught.value.missing) == {
        f"{kind}:{prefix}"
        for kind in ("template", "webhook_filter")
        for prefix in ("SUB", "TOP", "INV")
    }


async def test_provider_without_a_template_checklist_is_never_ready(app: App) -> None:
    with pytest.raises(ReadinessChecklistIncomplete) as caught:
        await record(app.module, app)
    assert caught.value.missing == ("template_checklist",)


async def test_environment_must_be_the_connection_environment(app: App, ops: PaymentModule) -> None:
    with pytest.raises(OnboardingRejected) as caught:
        await record(ops, app, environment=Environment.LIVE)
    assert caught.value.code == "ENVIRONMENT_MISMATCH"


async def test_evidence_and_actor_are_required(app: App, ops: PaymentModule) -> None:
    with pytest.raises(OnboardingRejected) as caught:
        await record(ops, app, evidence_ref="")
    assert caught.value.code == "EVIDENCE_REQUIRED"
    with pytest.raises(OnboardingRejected) as caught:
        await record(ops, app, verified_by="")
    assert caught.value.code == "ACTOR_REQUIRED"


async def test_unknown_or_legacy_profile_cannot_be_made_ready(app: App, ops: PaymentModule) -> None:
    with pytest.raises(ReferenceProfileRejected) as caught:
        await record(ops, app, profile_version=5)
    assert caught.value.code == "PROFILE_NOT_FOUND"
    await ops.import_legacy_reference_profile.execute(6, ACTOR)
    with pytest.raises(ReferenceProfileRejected) as caught:
        await record(ops, app, profile_version=6)
    assert caught.value.code == "PROFILE_NOT_GENERATED"


async def test_ready_for_a_draft_does_not_activate_the_connection(
    app: App, ops: PaymentModule
) -> None:
    await ops.set_connection_status.execute(
        app.m1.tenant_id, app.m1.connection_id, ConnectionStatus.NOT_READY, ACTOR, "rotation"
    )
    await ops.create_reference_profile.execute(
        2, {"subscription": "SUB", "topup": "TOP", "invoice": "INV"}, 10, "ABCDEFGHJK", ACTOR
    )

    result = await record(ops, app, profile_version=2)

    assert result.connection_status == ConnectionStatus.NOT_READY
