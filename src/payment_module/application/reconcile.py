"""Reconciliation through the provider transaction API.

Each API row becomes one ``api`` observation (idempotent on its source id). Under the scoped
match-key lock the observation is linked, in order:

1. to the fact that already carries this API id (``same_source_id``);
2. to the one fact a webhook recorded with the same bank reference, account, direction and
   amount (``bank_reference``), recording the API id on it;
3. otherwise it stays ``unlinked``; several candidates make it ``ambiguous`` (metric only).

An observation still ``unlinked`` after ``reconcile_grace_seconds`` is picked up again by the
grace pass, whatever the read cursor did meanwhile, and gets a fact of its own
(``first_source=reconcile``): outgoing or unknown money is ``not_applicable``; incoming money
opens an ``UNVERIFIED_IDENTITY`` review in ``detect_only`` and goes through the matching chain
in ``auto_settle``. An API receipt carries no webhook signature and no verified provider time,
so its observation time stands in for the receipt time: an intent that expired before the
money was first seen is never auto-settled.

A page's checkpoint is stored in the same transaction as its observations, so a crash or a
failed read never moves the cursor past rows that were not recorded.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from payment_module.application.apply_match_outcome import ApplyMatchOutcome
from payment_module.application.config import PaymentModuleConfig
from payment_module.application.match_context import (
    link_by_bank_reference,
    load_match_context,
    resolve_receiver,
)
from payment_module.application.process_inbox import dedup_key
from payment_module.domain.enums import (
    ConnectionStatus,
    Direction,
    FirstSource,
    IdentityKind,
    LinkMethod,
    LinkStatus,
    ObservationSource,
    ReceiptTimeSource,
    ReconcileMode,
    ReconciliationRunStatus,
    ReviewReason,
    SettlementOrigin,
)
from payment_module.domain.intent import IntentView
from payment_module.domain.matching.match_transaction import MatchTransaction
from payment_module.domain.money import AmountVnd
from payment_module.domain.reference import tokens_from
from payment_module.domain.review import MatchOutcome, NotApplicable, Review
from payment_module.domain.transaction import TransactionView
from payment_module.ports.clock import Clock
from payment_module.ports.metrics import MetricsSink, increment_safely
from payment_module.ports.provider import NormalizedObservation
from payment_module.ports.reader import TransactionReader, TransactionReadError, Window
from payment_module.ports.resolvers import ProviderConnection, SecretResolver
from payment_module.ports.unit_of_work import (
    NewProviderTransaction,
    ObservationView,
    UnitOfWork,
    UnitOfWorkFactory,
)

logger = logging.getLogger(__name__)


class LinkResult(StrEnum):
    """What happened to one API observation; also the keys of a run's ``counts``."""

    SEEN = "seen"
    LINKED = "linked"
    UNLINKED = "unlinked"
    AMBIGUOUS = "ambiguous"
    CREATED = "created"
    SKIPPED = "skipped"


class ReconcileStatus(StrEnum):
    RECONCILED = "reconciled"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """``skipped`` carries a ``reason`` (``not_found``, ``disabled``); ``counts`` sums the
    page reads and the grace pass."""

    connection_id: UUID
    status: ReconcileStatus
    reason: str | None = None
    counts: Mapping[str, int] = field(default_factory=dict)


class Reconcile:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        readers: Mapping[str, TransactionReader],
        secret_resolver: SecretResolver,
        clock: Clock,
        match: MatchTransaction,
        apply_outcome: ApplyMatchOutcome,
        metrics: MetricsSink,
        config: PaymentModuleConfig,
    ) -> None:
        self._uow_factory = uow_factory
        self._readers = readers
        self._secrets = secret_resolver
        self._clock = clock
        self._match = match
        self._apply = apply_outcome
        self._metrics = metrics
        self._config = config

    async def execute(
        self, tenant_id: str, connection_id: UUID, now: datetime | None = None
    ) -> ReconcileResult:
        """Read one page per bound account, then decide observations past grace."""
        now = now or self._clock.now()
        async with self._uow_factory() as uow:
            connection = await uow.connections.get(tenant_id, connection_id)
            targets = (
                []
                if connection is None
                else await uow.reconciliation_runs.read_targets(connection_id)
            )
            await uow.commit()
        if connection is None:
            return ReconcileResult(connection_id, ReconcileStatus.SKIPPED, "not_found")
        if connection.status == ConnectionStatus.DISABLED:
            return ReconcileResult(connection_id, ReconcileStatus.SKIPPED, "disabled")
        counts: Counter[str] = Counter()
        reader = self._readers.get(connection.provider)
        if reader is not None and connection.api_credential_ref is not None:
            credential = await self._secrets.api_credential(connection)
            if credential is None:
                counts["no_credential"] += 1
                logger.warning(
                    "payment_reconcile_no_credential", extra={"connection_id": str(connection.id)}
                )
            else:
                for target in targets:
                    if target.provider_account_ref is None:
                        # Without the provider's account id a company credential would read
                        # every account of the company, so the account is not read at all.
                        counts["unmapped_accounts"] += 1
                        continue
                    counts.update(
                        await self._read_page(
                            connection, reader, credential, target.provider_account_ref, now
                        )
                    )
        counts.update(await self._grace_pass(connection, now))
        return ReconcileResult(connection_id, ReconcileStatus.RECONCILED, counts=dict(counts))

    async def _read_page(
        self,
        connection: ProviderConnection,
        reader: TransactionReader,
        credential: str,
        account_ref: str,
        now: datetime,
    ) -> Counter[str]:
        async with self._uow_factory() as uow:
            checkpoint = await uow.reconciliation_runs.last_checkpoint(connection.id, account_ref)
            await uow.commit()
        if checkpoint is None:
            window = Window(now - timedelta(hours=self._config.reconcile_window_hours), now)
            cursor = None
        else:
            window, cursor = Window(checkpoint.window_from, checkpoint.window_to), checkpoint.cursor
        try:
            page = await reader.list_page(
                connection, credential, cursor, window, account_ref=account_ref
            )
        except TransactionReadError as exc:
            await self._record_failed_read(connection, account_ref, window, cursor, now, exc)
            return Counter(read_failed=1)

        counts: Counter[str] = Counter(rows=len(page.observations), invalid=page.invalid_rows)
        missing_webhooks = 0
        async with self._uow_factory() as uow:
            run_id = await uow.reconciliation_runs.start(
                tenant_id=connection.tenant_id,
                connection_id=connection.id,
                window_from=window.start,
                window_to=window.end,
                started_at=now,
                cursor=cursor,
                account_ref=account_ref,
            )
            for obs in page.observations:
                result = await self._record(uow, connection, run_id, obs, now)
                counts[result.value] += 1
                if result == LinkResult.UNLINKED and obs.source_tx_id in page.webhook_success_ids:
                    missing_webhooks += 1
            await uow.reconciliation_runs.finish(
                run_id,
                status=ReconciliationRunStatus.COMPLETED,
                counts=dict(counts),
                cursor=page.next_cursor,
                finished_at=now,
            )
            await uow.commit()
        logger.info(
            "payment_reconcile_page",
            extra={
                "connection_id": str(connection.id),
                "run_id": str(run_id),
                "counts": dict(counts),
            },
        )
        tags = {"tenant_id": connection.tenant_id, "connection_id": str(connection.id)}
        for name, amount in (
            ("reconcile_ambiguous_total", counts[LinkResult.AMBIGUOUS.value]),
            ("reconcile_row_invalid_total", page.invalid_rows),
            # The provider says a webhook was delivered but none arrived: a locator or
            # filter misconfiguration, alerted per connection, never decided per row.
            ("reconcile_webhook_success_but_missing_total", missing_webhooks),
        ):
            for _ in range(amount):
                increment_safely(self._metrics, name, tags)
        return counts

    async def _record_failed_read(
        self,
        connection: ProviderConnection,
        account_ref: str,
        window: Window,
        cursor: str | None,
        now: datetime,
        exc: TransactionReadError,
    ) -> None:
        logger.warning(
            "payment_reconcile_read_failed",
            extra={"connection_id": str(connection.id), "reason": exc.reason},
        )
        async with self._uow_factory() as uow:
            run_id = await uow.reconciliation_runs.start(
                tenant_id=connection.tenant_id,
                connection_id=connection.id,
                window_from=window.start,
                window_to=window.end,
                started_at=now,
                cursor=cursor,
                account_ref=account_ref,
            )
            await uow.reconciliation_runs.finish(
                run_id,
                status=ReconciliationRunStatus.FAILED,
                counts={},
                cursor=cursor,
                finished_at=now,
                last_error=exc.reason,
            )
            await uow.commit()
        increment_safely(
            self._metrics,
            "reconcile_read_failed_total",
            {"tenant_id": connection.tenant_id, "connection_id": str(connection.id)},
        )

    async def _record(
        self,
        uow: UnitOfWork,
        connection: ProviderConnection,
        run_id: UUID,
        obs: NormalizedObservation,
        now: datetime,
    ) -> LinkResult:
        observation_id = await uow.observations.add_if_absent(
            tenant_id=connection.tenant_id,
            environment=connection.environment,
            connection_id=connection.id,
            provider=connection.provider,
            source=ObservationSource.API,
            source_tx_id=obs.source_tx_id,
            reported_account_key=obs.reported_account_key,
            amount=obs.amount,
            direction=obs.direction,
            observed_at=now,
            reconciliation_run_id=run_id,
            bank_reference=obs.bank_reference,
            occurred_at=obs.provider_time,
            code=obs.code,
            memo=obs.content,
            link_status=LinkStatus.UNLINKED,
            purge_after=self._config.purge_after(now),
        )
        if observation_id is None:
            return LinkResult.SEEN
        return await self._link(
            uow,
            connection,
            observation_id,
            source_tx_id=obs.source_tx_id,
            reported_account_key=obs.reported_account_key,
            bank_reference=obs.bank_reference,
            direction=obs.direction,
            amount=obs.amount,
            now=now,
        )

    async def _link(
        self,
        uow: UnitOfWork,
        connection: ProviderConnection,
        observation_id: UUID,
        *,
        source_tx_id: str,
        reported_account_key: str,
        bank_reference: str | None,
        direction: Direction,
        amount: AmountVnd,
        now: datetime,
    ) -> LinkResult:
        """Link an ``unlinked`` API observation to an existing fact, under the match-key lock."""
        if bank_reference is not None:
            await uow.transactions.lock_match_key(
                connection.tenant_id,
                connection.environment,
                connection.provider,
                reported_account_key,
                bank_reference,
            )
        tx = await uow.transactions.find_by_source_id(
            connection.tenant_id,
            connection.environment,
            connection.provider,
            reported_account_key,
            IdentityKind.API_ID,
            source_tx_id,
        )
        method = LinkMethod.SAME_SOURCE_ID
        if tx is None:
            attempt = await link_by_bank_reference(
                uow,
                connection,
                reported_account_key=reported_account_key,
                bank_reference=bank_reference,
                direction=direction,
                amount=amount,
                source_tx_id=source_tx_id,
                missing=IdentityKind.API_ID,
                created_after=now - timedelta(hours=self._config.reconcile_window_hours),
            )
            if attempt.ambiguous:
                await uow.observations.set_link(observation_id, None, None, LinkStatus.AMBIGUOUS)
                return LinkResult.AMBIGUOUS
            if attempt.tx is None:
                return LinkResult.UNLINKED
            tx, method = attempt.tx, LinkMethod.BANK_REFERENCE
        await uow.observations.set_link(observation_id, tx.id, method, LinkStatus.LINKED)
        return LinkResult.LINKED

    async def _grace_pass(self, connection: ProviderConnection, now: datetime) -> Counter[str]:
        cutoff = now - timedelta(seconds=self._config.reconcile_grace_seconds)
        async with self._uow_factory() as uow:
            due = await uow.observations.list_unlinked_before(
                connection.id, cutoff, self._config.reconcile_page_size
            )
            await uow.commit()
        counts: Counter[str] = Counter()
        for observation_id in due:
            try:
                result = await self._decide_after_grace(connection, observation_id, now)
            except Exception as exc:
                # The observation stays unlinked, so the next tick tries again.
                logger.error(
                    "payment_reconcile_grace_failed",
                    extra={"observation_id": str(observation_id), "error": type(exc).__name__},
                )
                increment_safely(
                    self._metrics,
                    "reconcile_grace_failed_total",
                    {"tenant_id": connection.tenant_id, "connection_id": str(connection.id)},
                )
                counts["grace_failed"] += 1
                continue
            counts[result.value] += 1
        return counts

    async def _decide_after_grace(
        self, connection: ProviderConnection, observation_id: UUID, now: datetime
    ) -> LinkResult:
        async with self._uow_factory() as uow:
            obs = await uow.observations.get_unlinked_for_update(observation_id)
            if obs is None:
                return LinkResult.SKIPPED
            result = await self._link(
                uow,
                connection,
                obs.id,
                source_tx_id=obs.source_tx_id,
                reported_account_key=obs.reported_account_key,
                bank_reference=obs.bank_reference,
                direction=obs.direction,
                amount=obs.amount,
                now=now,
            )
            if result != LinkResult.UNLINKED:
                await uow.commit()
                return result
            tx, created = await self._create_fact(uow, connection, obs)
            await uow.observations.set_link(
                obs.id, tx.id, LinkMethod.SAME_SOURCE_ID, LinkStatus.LINKED
            )
            if not created:
                await uow.commit()
                return LinkResult.LINKED
            outcome, intents = await self._outcome(uow, connection, tx, obs)
            view = await self._apply.apply(
                uow, tx=tx, outcome=outcome, intents=intents, origin=SettlementOrigin.AUTO
            )
            await uow.commit()
        self._apply.after_commit(view, outcome)
        return LinkResult.CREATED

    async def _create_fact(
        self, uow: UnitOfWork, connection: ProviderConnection, obs: ObservationView
    ) -> tuple[TransactionView, bool]:
        receiver = await resolve_receiver(uow, connection, obs.reported_account_key)
        new = NewProviderTransaction(
            tenant_id=connection.tenant_id,
            environment=connection.environment,
            provider=connection.provider,
            provider_account_key=obs.reported_account_key,
            dedup_key=dedup_key(
                connection.provider, obs.reported_account_key, IdentityKind.API_ID, obs.source_tx_id
            ),
            identity_kind=IdentityKind.API_ID,
            identity_value=obs.source_tx_id,
            receiving_account_id=None if receiver is None else receiver.id,
            merchant_id=None if receiver is None else receiver.merchant_id,
            amount=obs.amount,
            direction=obs.direction,
            bank_reference=obs.bank_reference,
            first_source=FirstSource.RECONCILE,
            occurred_at=obs.occurred_at,
        )
        tx, created = await uow.transactions.insert_or_get_by_dedup_key(new)
        if created:
            return tx, True
        locked = await uow.transactions.get_for_update(tx.tenant_id, tx.environment, tx.id)
        assert locked is not None
        return locked, False

    async def _outcome(
        self,
        uow: UnitOfWork,
        connection: ProviderConnection,
        tx: TransactionView,
        obs: ObservationView,
    ) -> tuple[MatchOutcome, Mapping[UUID, IntentView]]:
        if tx.direction != Direction.IN:
            return NotApplicable(), {}
        if connection.reconcile_mode != ReconcileMode.AUTO_SETTLE:
            # Without verified evidence an API receipt never counts as a signed webhook.
            return Review(ReviewReason.UNVERIFIED_IDENTITY, details={"source": "api"}), {}
        ctx, intents = await load_match_context(
            uow,
            tx=tx,
            connection=connection,
            tokens=tokens_from(obs.code, obs.memo),
            effective_received_at=obs.observed_at,
            time_source=ReceiptTimeSource.OBSERVED,
        )
        return self._match.decide(ctx), intents


class ReconcileScheduler:
    """Round robin over reconcilable connections of every tenant, one connection per tick.

    Fairness between merchants lives here; the provider's per-IP rate limit is enforced by
    the reader's process-wide limiter.
    """

    def __init__(self, uow_factory: UnitOfWorkFactory, reconcile: Reconcile) -> None:
        self._uow_factory = uow_factory
        self._reconcile = reconcile
        self._last: UUID | None = None

    async def tick(self) -> ReconcileResult | None:
        """Reconcile the connection after the last one served; ``None`` when there is none."""
        async with self._uow_factory() as uow:
            connections = await uow.connections.list_reconcilable()
            await uow.commit()
        chosen = _next_after(connections, self._last)
        if chosen is None:
            return None
        self._last = chosen.id
        return await self._reconcile.execute(chosen.tenant_id, chosen.id)


def _next_after(
    connections: Sequence[ProviderConnection], last: UUID | None
) -> ProviderConnection | None:
    if not connections:
        return None
    ordered = sorted(connections, key=lambda connection: connection.id)
    if last is None:
        return ordered[0]
    for connection in ordered:
        if connection.id > last:
            return connection
    return ordered[0]
