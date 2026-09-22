"""Reading back the observations of a processed fact."""

from __future__ import annotations

from datetime import timedelta

import pytest

from fakes.payment_app import App
from payment_module.domain.enums import (
    Direction,
    LinkMethod,
    LinkStatus,
    ObservationSource,
)
from payment_module.domain.money import AmountVnd

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


async def test_observations_of_a_processed_fact_are_listed_oldest_first(app: App) -> None:
    created = await app.intent()
    memo = f"thanh toan {created.payment_reference}"
    await app.pay(code=created.payment_reference, content=memo)
    [fact] = await app.rows(app.tables.provider_transactions)

    later = app.clock.now() + timedelta(minutes=5)
    async with app.uow_factory()() as uow:
        run_id = await uow.reconciliation_runs.start(
            tenant_id=app.m1.tenant_id,
            connection_id=app.m1.connection_id,
            window_from=later - timedelta(hours=1),
            window_to=later,
            started_at=later,
        )
        await uow.observations.add(
            tenant_id=app.m1.tenant_id,
            environment=app.m1.environment,
            connection_id=app.m1.connection_id,
            provider="fake",
            source=ObservationSource.API,
            source_tx_id="api-1",
            reconciliation_run_id=run_id,
            reported_account_key=fact.provider_account_key,
            amount=AmountVnd(150_000),
            direction=Direction.IN,
            observed_at=later,
            transaction_id=fact.id,
            link_method=LinkMethod.SAME_SOURCE_ID,
            link_status=LinkStatus.LINKED,
        )
        await uow.commit()

    async with app.uow_factory()() as uow:
        webhook, api = await uow.observations.list_for_transaction(fact.id)

    assert webhook.source is ObservationSource.WEBHOOK
    assert webhook.transaction_id == fact.id
    assert webhook.connection_id == app.m1.connection_id
    assert webhook.tenant_id == app.m1.tenant_id
    assert webhook.environment is app.m1.environment
    assert webhook.code == created.payment_reference
    assert webhook.memo == memo
    assert webhook.amount == AmountVnd(150_000)
    assert webhook.direction is Direction.IN
    assert webhook.link_method is LinkMethod.SAME_SOURCE_ID
    assert webhook.link_status is LinkStatus.LINKED
    assert webhook.reported_account_key == fact.provider_account_key
    assert api.source is ObservationSource.API
    assert api.code is None
    assert webhook.observed_at < api.observed_at
