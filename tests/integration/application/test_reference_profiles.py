"""Reference profile lifecycle: create, strict activation, atomic rotation, retirement and
legacy import, with readiness that covers every prefix still accepted."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
import sqlalchemy as sa

from fakes.fake_checklist import SHARED_ITEMS, FakeChecklist
from fakes.payment_app import App, Scope
from payment_module.application.config import PaymentModuleConfig
from payment_module.builder import PaymentModule
from payment_module.domain.enums import ConnectionStatus, MatchState, ProfileStatus
from payment_module.domain.errors import (
    IntentRejected,
    InvalidReferenceProfile,
    PrefixOverlap,
    ProfileActivationBlocked,
    ReadinessChecklistIncomplete,
    ReferenceProfileRejected,
)
from payment_module.domain.reference import PrefixShape

pytestmark = [pytest.mark.integration, pytest.mark.postgres]

ACTOR = "ops@example.test"
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
V1_PREFIXES = {"subscription": "SUB", "topup": "TOP"}


@pytest.fixture
async def ops(app: App) -> PaymentModule:
    """Profile v1 active with ``subscription/SUB`` and ``topup/TOP``, suffix length 24."""
    profiles = app.tables.reference_profiles
    prefixes = app.tables.reference_profile_prefixes
    await app.execute(sa.update(profiles).where(profiles.c.version == 1).values(suffix_length=24))
    await app.execute(sa.delete(prefixes).where(prefixes.c.name == "invoice"))
    return app.build(template_checklists={"fake": FakeChecklist()})


def confirmed(*prefixes: str, missing: tuple[str, ...] = ()) -> dict[str, bool]:
    keys = [*SHARED_ITEMS]
    for prefix in prefixes:
        keys += [f"template:{prefix}", f"webhook_filter:{prefix}"]
    return {key: True for key in keys if key not in missing}


def scopes(app: App) -> list[Scope]:
    return [app.m1, app.m1_live, app.m2, app.mb]


async def ready(ops: PaymentModule, scope: Scope, version: int, *prefixes: str):
    return await ops.record_connection_readiness.execute(
        scope.tenant_id,
        scope.connection_id,
        version,
        scope.environment,
        confirmed(*(prefixes or ("SUB", "TOP"))),
        "screenshots/2026-09-22.png",
        ACTOR,
    )


async def create(ops: PaymentModule, version: int, prefixes=None, suffix_length: int = 20):
    return await ops.create_reference_profile.execute(
        version, prefixes or V1_PREFIXES, suffix_length, ALPHABET, ACTOR
    )


async def statuses(app: App) -> dict[int, str]:
    rows = await app.rows(app.tables.reference_profiles)
    return {row.version: row.status for row in rows}


async def test_nested_prefix_in_version_rejected(app: App, ops: PaymentModule) -> None:
    with pytest.raises(PrefixOverlap):
        await create(ops, 2, {"subscription": "SUB", "subscription_extra": "SUBX"})
    with pytest.raises(PrefixOverlap):
        await create(ops, 2, {"subscription": "SUB", "same": "SUB"})
    with pytest.raises(InvalidReferenceProfile):
        await create(ops, 2, {"subscription": "SUBSCR"})
    assert await statuses(app) == {1: ProfileStatus.ACTIVE.value}


async def test_same_prefix_new_suffix_length_allowed(app: App, ops: PaymentModule) -> None:
    created = await create(ops, 2, suffix_length=20)

    assert created.profile.status == ProfileStatus.DRAFT
    assert {(a.candidate.prefix, a.accepted.version) for a in created.advisories} == {
        ("SUB", 1),
        ("TOP", 1),
    }
    fake = FakeChecklist()
    module = app.build(template_checklists={"fake": fake})
    await ready(module, app.m1, 2)
    version, accepted = fake.calls[-1]
    assert version == 2
    assert {(s.prefix, s.suffix_length, s.version) for s in accepted} == {
        ("SUB", 24, 1),
        ("TOP", 24, 1),
    }
    template = fake.item(created.profile, "template:SUB", accepted)
    assert (template.params["suffix_min"], template.params["suffix_max"]) == ("20", "24")


async def test_readiness_requires_every_named_prefix(app: App, ops: PaymentModule) -> None:
    await create(ops, 2)

    with pytest.raises(ReadinessChecklistIncomplete) as caught:
        await ops.record_connection_readiness.execute(
            app.m1.tenant_id,
            app.m1.connection_id,
            2,
            app.m1.environment,
            confirmed("SUB", "TOP", missing=("webhook_filter:TOP",)),
            "evidence",
            ACTOR,
        )
    assert caught.value.missing == ("webhook_filter:TOP",)
    unconfirmed = confirmed("SUB", "TOP") | {"template:SUB": False}
    with pytest.raises(ReadinessChecklistIncomplete) as caught:
        await ops.record_connection_readiness.execute(
            app.m1.tenant_id, app.m1.connection_id, 2, app.m1.environment, unconfirmed, "e", ACTOR
        )
    assert caught.value.missing == ("template:SUB",)
    r = app.tables.connection_reference_readiness
    assert await app.count(r) == 0


async def test_candidate_that_drops_a_prefix_still_needs_its_filter(
    app: App, ops: PaymentModule
) -> None:
    await create(ops, 2, {"subscription": "SUB", "credit": "CRD"})

    with pytest.raises(ReadinessChecklistIncomplete) as caught:
        await ready(ops, app.m1, 2, "SUB", "CRD")
    assert set(caught.value.missing) == {"template:TOP", "webhook_filter:TOP"}
    result = await ready(ops, app.m1, 2, "SUB", "CRD", "TOP")
    assert result.readiness.status == "ready"


async def test_activate_blocked_until_ready(app: App, ops: PaymentModule) -> None:
    await create(ops, 2)
    for scope in (app.m1, app.m1_live, app.m2):
        result = await ready(ops, scope, 2)
        assert result.connection_status == ConnectionStatus.ACTIVE

    with pytest.raises(ProfileActivationBlocked) as caught:
        await ops.activate_reference_profile.execute(2, ACTOR)
    assert caught.value.connection_ids == (app.mb.connection_id,)
    assert await statuses(app) == {1: "active", 2: "draft"}

    await ops.set_connection_status.execute(
        app.mb.tenant_id,
        app.mb.connection_id,
        ConnectionStatus.NOT_READY,
        ACTOR,
        "dashboard template not updated yet",
    )
    activated = await ops.activate_reference_profile.execute(2, ACTOR)

    assert activated.status == ProfileStatus.ACTIVE
    assert await statuses(app) == {1: "accepted_legacy", 2: "active"}
    profiles = app.tables.reference_profiles
    [row] = await app.rows(profiles, profiles.c.version == 2)
    assert (row.activated_by, row.activated_at) == (ACTOR, app.clock.now())
    c = app.tables.provider_connections
    [mb] = await app.rows(c, c.c.id == app.mb.connection_id)
    assert (mb.status, mb.status_changed_by) == ("not_ready", ACTOR)
    assert mb.status_changed_reason == "dashboard template not updated yet"

    fresh = await ops.create_intent.execute(app.command())
    assert fresh.intent.payment_reference.startswith("SUB")
    assert len(fresh.intent.payment_reference) == 23
    intents = app.tables.payment_intents
    [stored] = await app.rows(intents, intents.c.id == fresh.intent.id)
    assert stored.reference_profile_version == 2
    with pytest.raises(IntentRejected):
        await ops.create_intent.execute(app.command(app.mb))


async def rotate(app: App, ops: PaymentModule, version: int = 2) -> None:
    for scope in scopes(app):
        await ready(ops, scope, version)
    await ops.activate_reference_profile.execute(version, ACTOR)


async def test_legacy_code_still_matches_after_rotation(app: App, ops: PaymentModule) -> None:
    old = await ops.create_intent.execute(app.command(amount_vnd=210_000))
    assert len(old.payment_reference) == 27
    intents = app.tables.payment_intents
    [before] = await app.rows(intents, intents.c.id == old.intent.id)
    await create(ops, 2)
    await rotate(app, ops)

    await app.pay(code=old.payment_reference, amount=210_000)

    t = app.tables.provider_transactions
    [fact] = await app.rows(t)
    assert fact.match_state == MatchState.SETTLED.value
    [after] = await app.rows(intents, intents.c.id == old.intent.id)
    assert after.status == "paid"
    assert after.payment_reference == before.payment_reference
    assert after.beneficiary_snapshot == before.beneficiary_snapshot
    assert after.reference_profile_version == 1


async def test_concurrent_activations_leave_exactly_one_active(
    app: App, ops: PaymentModule
) -> None:
    await create(ops, 2)
    await create(ops, 3, suffix_length=22)
    for version in (2, 3):
        for scope in scopes(app):
            await ready(ops, scope, version)

    results = await asyncio.gather(
        ops.activate_reference_profile.execute(2, ACTOR),
        ops.activate_reference_profile.execute(3, ACTOR),
        ops.activate_reference_profile.execute(2, ACTOR),
        return_exceptions=True,
    )

    won = [r for r in results if not isinstance(r, Exception)]
    assert len(won) == 1
    assert all(
        isinstance(r, ProfileActivationBlocked | ReferenceProfileRejected)
        for r in results
        if isinstance(r, Exception)
    )
    current = await statuses(app)
    assert list(current.values()).count("active") == 1
    assert current[1] == "accepted_legacy"


async def test_activation_invalidates_readiness_of_other_drafts(
    app: App, ops: PaymentModule
) -> None:
    await create(ops, 2)
    await create(ops, 3, {"subscription": "SUB", "credit": "CRD"}, suffix_length=22)
    for scope in scopes(app):
        await ready(ops, scope, 3, "SUB", "CRD", "TOP")
    await rotate(app, ops, 2)

    with pytest.raises(ProfileActivationBlocked):
        await ops.activate_reference_profile.execute(3, ACTOR)
    r = app.tables.connection_reference_readiness
    [pending] = await app.rows(
        r, r.c.profile_version == 3, r.c.connection_id == app.m1.connection_id
    )
    assert pending.status == "pending"


async def test_only_a_draft_can_be_activated(app: App, ops: PaymentModule) -> None:
    with pytest.raises(ReferenceProfileRejected) as caught:
        await ops.activate_reference_profile.execute(1, ACTOR)
    assert caught.value.code == "PROFILE_NOT_DRAFT"
    with pytest.raises(ReferenceProfileRejected) as caught:
        await ops.activate_reference_profile.execute(9, ACTOR)
    assert caught.value.code == "PROFILE_NOT_FOUND"
    with pytest.raises(ReferenceProfileRejected) as caught:
        await create(ops, 1)
    assert caught.value.code == "VERSION_EXISTS"


async def test_retire_waits_for_awaiting_intents(app: App, ops: PaymentModule) -> None:
    old = await ops.create_intent.execute(app.command())
    await create(ops, 2)
    await rotate(app, ops)

    with pytest.raises(ReferenceProfileRejected) as caught:
        await ops.retire_reference_profile.execute(1, ACTOR, "rotation finished")
    assert caught.value.code == "PROFILE_STILL_IN_USE"
    with pytest.raises(ReferenceProfileRejected) as caught:
        await ops.retire_reference_profile.execute(2, ACTOR, "wrong version")
    assert caught.value.code == "PROFILE_NOT_ACCEPTED_LEGACY"

    await ops.cancel_intent.execute(app.m1.tenant_id, old.intent.id, "customer left")
    retired = await ops.retire_reference_profile.execute(1, ACTOR, "rotation finished")

    assert retired.status == ProfileStatus.RETIRED
    profiles = app.tables.reference_profiles
    [row] = await app.rows(profiles, profiles.c.version == 1)
    assert (row.retired_by, row.retired_at) == (ACTOR, app.clock.now())


async def test_retired_version_leaves_the_checklist(app: App, ops: PaymentModule) -> None:
    await ready(ops, app.m1, 1)
    await create(ops, 2, {"subscription": "SUB", "credit": "CRD"})
    for scope in scopes(app):
        await ready(ops, scope, 2, "SUB", "CRD", "TOP")
    await ops.activate_reference_profile.execute(2, ACTOR)
    await ops.retire_reference_profile.execute(1, ACTOR, "rotation finished")
    await create(ops, 3, {"subscription": "SUB", "credit": "CRD"}, suffix_length=22)

    result = await ready(ops, app.m1, 3, "SUB", "CRD")

    assert result.readiness.status == "ready"
    r = app.tables.connection_reference_readiness
    [old] = await app.rows(r, r.c.profile_version == 1)
    assert old.status == "retired"


async def test_two_legacy_versions_need_an_explicit_version(app: App, ops: PaymentModule) -> None:
    for version in (7, 8):
        imported = await ops.import_legacy_reference_profile.execute(version, ACTOR)
        assert imported.status == ProfileStatus.ACCEPTED_LEGACY
    with pytest.raises(ReferenceProfileRejected) as caught:
        await ops.import_legacy_reference_profile.execute(8, ACTOR)
    assert caught.value.code == "VERSION_EXISTS"

    with pytest.raises(IntentRejected) as rejected:
        await ops.create_intent.execute(
            app.command(prefix_name=None, reference_override="OLDSHOP-42")
        )
    assert rejected.value.code == "LEGACY_PROFILE_REQUIRED"
    created = await ops.create_intent.execute(
        app.command(prefix_name=None, reference_override="OLDSHOP-42", reference_profile_version=8)
    )
    assert created.payment_reference == "OLDSHOP-42"
    intents = app.tables.payment_intents
    [row] = await app.rows(intents, intents.c.id == created.intent.id)
    assert (row.reference_profile_version, row.reference_prefix_name) == (8, None)


async def test_shapes_passed_to_the_checklist_skip_drafts_and_retired(
    app: App, ops: PaymentModule
) -> None:
    await create(ops, 2)
    await create(ops, 3, {"subscription": "SUB"}, suffix_length=18)
    fake = FakeChecklist()
    module = app.build(template_checklists={"fake": fake})

    await ready(module, app.m1, 3, "SUB", "TOP")

    _, accepted = fake.calls[-1]
    assert {shape.version for shape in accepted} == {1}
    assert all(isinstance(shape, PrefixShape) for shape in accepted)


async def test_retire_waits_for_expired_intents_within_the_late_settlement_window(
    app: App, ops: PaymentModule
) -> None:
    """An expired intent still takes late money for ``late_settlement_days`` (30 by
    default), so its profile keeps its prefixes and filters until then."""
    old = await ops.create_intent.execute(app.command())
    await create(ops, 2)
    await rotate(app, ops)
    intents = app.tables.payment_intents
    await app.execute(
        sa.update(intents).where(intents.c.id == old.intent.id).values(status="expired")
    )
    [row] = await app.rows(intents, intents.c.id == old.intent.id)
    app.clock.current = row.expires_at + timedelta(days=30)

    with pytest.raises(ReferenceProfileRejected) as caught:
        await ops.retire_reference_profile.execute(1, ACTOR, "rotation finished")
    assert caught.value.code == "PROFILE_STILL_IN_USE"
    assert (await statuses(app))[1] == "accepted_legacy"

    app.clock.advance(seconds=1)
    retired = await ops.retire_reference_profile.execute(1, ACTOR, "rotation finished")
    assert retired.status == ProfileStatus.RETIRED


async def test_late_settlement_window_is_configurable(app: App, ops: PaymentModule) -> None:
    old = await ops.create_intent.execute(app.command())
    await create(ops, 2)
    await rotate(app, ops)
    intents = app.tables.payment_intents
    await app.execute(
        sa.update(intents).where(intents.c.id == old.intent.id).values(status="expired")
    )
    [row] = await app.rows(intents, intents.c.id == old.intent.id)
    app.clock.current = row.expires_at + timedelta(days=8)
    short = app.build(config=PaymentModuleConfig(worker_owner="w", late_settlement_days=7))

    retired = await short.retire_reference_profile.execute(1, ACTOR, "rotation finished")

    assert retired.status == ProfileStatus.RETIRED
