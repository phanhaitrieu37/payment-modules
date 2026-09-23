"""Worker loops: optional loops, best-effort steps, clean stop within one interval."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

from fakes.payment_app import RecordingMetrics
from payment_module.runtime import PaymentWorker, WorkerIntervals

FAST = WorkerIntervals(inbox_s=0.01, outbox_s=0.01, reconcile_s=0.01, purge_s=0.01)


class Step:
    """Counts calls; raises on the calls listed in ``fail_on`` (1-based)."""

    def __init__(self, fail_on: tuple[int, ...] = ()) -> None:
        self.calls = 0
        self.fail_on = fail_on

    async def __call__(self, *args: Any) -> None:
        self.calls += 1
        if self.calls in self.fail_on:
            raise RuntimeError("database unavailable")


def stub_module(*, with_outbox: bool = True, with_reconcile: bool = True, **steps: Step) -> Any:
    step = {name: steps.get(name, Step()) for name in ("inbox", "outbox", "reconcile", "purge")}
    return SimpleNamespace(
        metrics=RecordingMetrics(),
        process_inbox=SimpleNamespace(run_batch=step["inbox"]),
        dispatch_outbox=SimpleNamespace(run_batch=step["outbox"]) if with_outbox else None,
        reconcile_scheduler=SimpleNamespace(tick=step["reconcile"]) if with_reconcile else None,
        purge_expired_payloads=SimpleNamespace(drain=step["purge"]),
    )


async def run_until(worker: PaymentWorker, done: Any, timeout: float = 5.0) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    deadline = time.monotonic() + timeout
    while not done() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, 1.0)


def test_every_loop_runs_when_the_module_has_its_use_cases() -> None:
    worker = PaymentWorker(stub_module())
    assert set(worker.loops()) == {"inbox", "outbox", "reconcile", "purge"}


def test_outbox_and_reconcile_loops_are_skipped_without_their_use_cases() -> None:
    worker = PaymentWorker(stub_module(with_outbox=False, with_reconcile=False))
    assert set(worker.loops()) == {"inbox", "purge"}


def test_default_intervals() -> None:
    assert WorkerIntervals() == WorkerIntervals(
        inbox_s=1.0, outbox_s=1.0, reconcile_s=60.0, purge_s=3600.0
    )


async def test_a_failing_step_is_counted_and_the_loop_carries_on() -> None:
    inbox, outbox = Step(fail_on=(1,)), Step()
    module = stub_module(inbox=inbox, outbox=outbox)

    await run_until(PaymentWorker(module, FAST), lambda: inbox.calls >= 3 and outbox.calls >= 3)

    assert inbox.calls >= 3 and outbox.calls >= 3
    assert module.metrics.tags.count(("worker_loop_errors_total", {"loop": "inbox"})) == 1
    assert module.metrics.counts == {"worker_loop_errors_total": 1}


async def test_every_loop_keeps_failing_without_stopping_the_others() -> None:
    purge = Step(fail_on=tuple(range(1, 1000)))
    inbox = Step()
    module = stub_module(purge=purge, inbox=inbox)

    await run_until(PaymentWorker(module, FAST), lambda: purge.calls >= 3 and inbox.calls >= 3)

    assert module.metrics.counts["worker_loop_errors_total"] == purge.calls
    assert {tags["loop"] for _, tags in module.metrics.tags} == {"purge"}


async def test_stop_ends_every_loop_within_one_interval() -> None:
    inbox = Step()
    worker = PaymentWorker(stub_module(inbox=inbox), WorkerIntervals(0.5, 0.5, 0.5, 0.5))
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    while inbox.calls == 0:
        await asyncio.sleep(0.01)

    started = time.monotonic()
    stop.set()
    await asyncio.wait_for(task, 1.0)

    assert time.monotonic() - started < 0.5
    assert inbox.calls == 1


async def test_a_worker_stopped_before_start_runs_nothing() -> None:
    inbox = Step()
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(PaymentWorker(stub_module(inbox=inbox), FAST).run(stop), 1.0)
    assert inbox.calls == 0
