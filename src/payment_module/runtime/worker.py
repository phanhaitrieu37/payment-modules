"""Background loops of one module: inbox, outbox, reconciliation and payload purge.

Nothing starts on import. The host runs ``await PaymentWorker(module).run(stop_event)`` in
its lifespan or in a process of its own, and sets ``stop_event`` to stop; each loop finishes
its current step and exits within one interval. A failing step is logged and counted, and
the loop carries on: leases and retry state live in the database, so a step that died is
picked up again by the next one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from payment_module.builder import PaymentModule
from payment_module.ports.metrics import increment_safely

logger = logging.getLogger(__name__)

Step = Callable[[], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class WorkerIntervals:
    """Seconds between two steps of each loop."""

    inbox_s: float = 1.0
    outbox_s: float = 1.0
    reconcile_s: float = 60.0
    purge_s: float = 3600.0


class PaymentWorker:
    """The outbox loop runs only with an outbox publisher, the reconcile loop only with
    transaction readers (see :func:`~payment_module.builder.build_payment_module`)."""

    def __init__(self, module: PaymentModule, intervals: WorkerIntervals | None = None) -> None:
        self._module = module
        self._intervals = intervals or WorkerIntervals()

    def loops(self) -> dict[str, tuple[Step, float]]:
        """Loop name -> (one step, interval) for every loop this module supports."""
        module, intervals = self._module, self._intervals
        loops: dict[str, tuple[Step, float]] = {
            "inbox": (module.process_inbox.run_batch, intervals.inbox_s)
        }
        if module.dispatch_outbox is not None:
            loops["outbox"] = (module.dispatch_outbox.run_batch, intervals.outbox_s)
        if module.reconcile_scheduler is not None:
            loops["reconcile"] = (module.reconcile_scheduler.tick, intervals.reconcile_s)
        loops["purge"] = (module.purge_expired_payloads.drain, intervals.purge_s)
        return loops

    async def run(self, stop_event: asyncio.Event) -> None:
        """Run every loop until ``stop_event`` is set."""
        async with asyncio.TaskGroup() as group:
            for name, (step, interval) in self.loops().items():
                group.create_task(self._loop(name, step, interval, stop_event))

    async def _loop(self, name: str, step: Step, interval: float, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await step()
            except Exception as exc:
                logger.error(
                    "payment_worker_loop_failed",
                    extra={"loop": name, "error": type(exc).__name__},
                )
                increment_safely(self._module.metrics, "worker_loop_errors_total", {"loop": name})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), interval)
