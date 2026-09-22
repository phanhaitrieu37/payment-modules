"""Wire the use cases once; the result holds them and has no business methods of its own."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from payment_module.application.apply_match_outcome import ApplyMatchOutcome
from payment_module.application.cancel_intent import CancelIntent
from payment_module.application.config import PaymentModuleConfig
from payment_module.application.create_intent import CreateIntent
from payment_module.application.dispatch_outbox import DispatchOutbox, RequeueOutbox
from payment_module.application.get_intent_status import GetIntentStatus
from payment_module.application.ingest_webhook import IngestWebhook
from payment_module.application.process_inbox import ProcessInbox
from payment_module.application.requeue_inbox import RequeueInbox
from payment_module.domain.matching.exact_amount_policy import ExactAmountPolicy, MatchingPolicy
from payment_module.domain.matching.match_transaction import MatchTransaction
from payment_module.ports.clock import Clock
from payment_module.ports.handlers import (
    NoOpOutcomeObserver,
    NoOpSettlementHandler,
    OutcomeObserver,
    SettlementHandler,
)
from payment_module.ports.metrics import MetricsSink, NoOpMetricsSink
from payment_module.ports.provider import PaymentProvider
from payment_module.ports.publisher import OutboxPublisher
from payment_module.ports.reference_generator import PaymentReferenceGenerator
from payment_module.ports.resolvers import SecretResolver
from payment_module.ports.unit_of_work import UnitOfWorkFactory
from payment_module.reference.random_suffix_generator import RandomSuffixGenerator


@dataclass(frozen=True, slots=True)
class PaymentModule:
    """``dispatch_outbox`` is ``None`` unless an :class:`OutboxPublisher` was given."""

    config: PaymentModuleConfig
    uow_factory: UnitOfWorkFactory
    clock: Clock
    providers: Mapping[str, PaymentProvider]
    match: MatchTransaction
    apply_outcome: ApplyMatchOutcome
    # intents
    create_intent: CreateIntent
    get_intent_status: GetIntentStatus
    cancel_intent: CancelIntent
    # processing
    ingest_webhook: IngestWebhook
    process_inbox: ProcessInbox
    dispatch_outbox: DispatchOutbox | None
    requeue_inbox: RequeueInbox
    requeue_outbox: RequeueOutbox
    # operator
    # reconcile


def build_payment_module(
    config: PaymentModuleConfig,
    uow_factory: UnitOfWorkFactory,
    provider_registry: Mapping[str, PaymentProvider],
    secret_resolver: SecretResolver,
    clock: Clock,
    policy: MatchingPolicy | None = None,
    settlement_handler: SettlementHandler | None = None,
    outcome_observer: OutcomeObserver | None = None,
    reference_generator: PaymentReferenceGenerator | None = None,
    metrics: MetricsSink | None = None,
    outbox_publisher: OutboxPublisher | None = None,
) -> PaymentModule:
    providers = dict(provider_registry)
    metrics = metrics or NoOpMetricsSink()
    match = MatchTransaction(policy or ExactAmountPolicy())
    apply_outcome = ApplyMatchOutcome(
        settlement_handler or NoOpSettlementHandler(),
        outcome_observer or NoOpOutcomeObserver(),
        metrics,
        clock,
    )
    return PaymentModule(
        config=config,
        uow_factory=uow_factory,
        clock=clock,
        providers=providers,
        match=match,
        apply_outcome=apply_outcome,
        # intents
        create_intent=CreateIntent(
            uow_factory, providers, reference_generator or RandomSuffixGenerator(), clock
        ),
        get_intent_status=GetIntentStatus(uow_factory, clock),
        cancel_intent=CancelIntent(uow_factory),
        # processing
        ingest_webhook=IngestWebhook(
            uow_factory, providers, secret_resolver, clock, metrics, config
        ),
        process_inbox=ProcessInbox(
            uow_factory, providers, clock, match, apply_outcome, metrics, config
        ),
        dispatch_outbox=(
            None
            if outbox_publisher is None
            else DispatchOutbox(uow_factory, outbox_publisher, clock, config)
        ),
        requeue_inbox=RequeueInbox(uow_factory),
        requeue_outbox=RequeueOutbox(uow_factory),
        # operator
        # reconcile
    )
