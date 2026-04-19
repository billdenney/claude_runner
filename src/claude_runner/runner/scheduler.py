"""Main scheduler loop — pulls from catalog, gates on budget, dispatches."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from claude_runner.budget.circuit_breaker import CircuitBreaker
from claude_runner.budget.controller import DecisionKind, TokenBudgetController
from claude_runner.config import Settings
from claude_runner.models import DispatchResult, RunRecord, StopReason, TaskStatus
from claude_runner.notify.emitter import EventEmitter
from claude_runner.runner.backend import RunnerBackend
from claude_runner.state.store import StateStore
from claude_runner.todo.catalog import CatalogEntry, TodoCatalog
from claude_runner.todo.schema import TaskSpec

_log = logging.getLogger(__name__)


@dataclass(slots=True)
class SchedulerOutcome:
    completed: int
    failed: int
    breaker_tripped: bool
    breaker_reason: str | None


class Scheduler:
    def __init__(
        self,
        *,
        settings: Settings,
        catalog: TodoCatalog,
        backend: RunnerBackend,
        budget: TokenBudgetController,
        state_store: StateStore,
        emitter: EventEmitter,
        breaker: CircuitBreaker,
    ) -> None:
        self._settings = settings
        self._catalog = catalog
        self._backend = backend
        self._budget = budget
        self._state = state_store
        self._emitter = emitter
        self._breaker = breaker
        self._in_flight: dict[str, asyncio.Task[DispatchResult]] = {}
        self._estimates: dict[str, int] = {}

    async def run(self) -> SchedulerOutcome:
        completed = 0
        failed = 0

        self._emitter.emit("run_started", backend=self._backend.name)
        try:
            while True:
                if self._breaker.tripped():
                    break
                self._budget.refresh()
                ready = self._catalog.ready_tasks(in_flight_ids=set(self._in_flight))
                if not ready and not self._in_flight:
                    self._emitter.emit("queue_drained")
                    break

                target = self._budget.target_concurrency()
                slots = max(target - len(self._in_flight), 0)

                launched_any = False
                wait_until: datetime | None = None
                stop_now = False

                for entry in ready[:slots]:
                    decision = self._budget.may_start(entry.spec.estimated_input_tokens)
                    if decision.kind is DecisionKind.OK:
                        self._launch(entry)
                        launched_any = True
                    elif decision.kind is DecisionKind.WAIT:
                        wait_until = decision.wait_until
                        self._emitter.emit(
                            "budget_wait",
                            reason=decision.reason,
                            wait_until=(wait_until.isoformat() if wait_until else None),
                        )
                        break
                    else:
                        self._emitter.emit("budget_stop", reason=decision.reason)
                        stop_now = True
                        break

                if stop_now:
                    break

                if not self._in_flight:
                    if wait_until is not None:
                        await self._sleep_until(wait_until)
                        continue
                    if (
                        not launched_any
                    ):  # pragma: no branch - launched_any True implies in_flight non-empty
                        # Nothing ran, nothing in flight, but catalog claims tasks exist.
                        # Most likely all remaining tasks are blocked on failed deps.
                        break

                done = await self._await_any()
                for result in done:
                    if result.success:
                        completed += 1
                        self._breaker.record_success()
                    else:
                        failed += 1
                        self._breaker.record_failure()
                        self._emitter.emit(
                            "task_failed_summary",
                            task_id=result.task_id,
                            error=result.error,
                        )
                self._catalog.invalidate()
        finally:
            # Gracefully finish anything still running.
            await self._drain()

        breaker_state = self._breaker.state()
        self._emitter.emit(
            "run_finished",
            completed=completed,
            failed=failed,
            breaker_tripped=breaker_state.tripped,
            breaker_reason=breaker_state.reason,
        )
        return SchedulerOutcome(
            completed=completed,
            failed=failed,
            breaker_tripped=breaker_state.tripped,
            breaker_reason=breaker_state.reason,
        )

    def _launch(self, entry: CatalogEntry) -> None:
        spec = entry.spec
        estimate = spec.estimated_input_tokens
        self._estimates[spec.id] = estimate
        self._budget.reserve(estimate)

        state = self._state.load(spec.id)
        state.status = TaskStatus.QUEUED
        state.last_started_at = datetime.now(tz=UTC)
        self._state.save(state)

        self._emitter.emit("task_started", task_id=spec.id, effort=spec.effort.value)
        coro = self._run_one(spec)
        task = asyncio.create_task(coro, name=f"claude-runner:{spec.id}")
        self._in_flight[spec.id] = task

    async def _run_one(self, spec: TaskSpec) -> DispatchResult:
        result = await self._backend.run_task(spec)
        estimate = self._estimates.pop(spec.id, spec.estimated_input_tokens)
        self._budget.release(estimate)
        self._budget.record_usage(result.usage, duration_s=result.duration_s)

        state = self._state.load(spec.id)
        state.runs.append(
            RunRecord(
                attempt=state.attempts or 1,
                started_at=state.last_started_at or datetime.now(tz=UTC),
                finished_at=state.last_finished_at or datetime.now(tz=UTC),
                usage=result.usage,
                stop_reason=result.stop_reason,
                error=result.error,
            )
        )
        self._state.save(state)
        return result

    async def _await_any(self) -> list[DispatchResult]:
        if not self._in_flight:
            return []
        done, _ = await asyncio.wait(
            list(self._in_flight.values()),
            return_when=asyncio.FIRST_COMPLETED,
        )
        results: list[DispatchResult] = []
        for task in done:
            tid = next((k for k, v in self._in_flight.items() if v is task), None)
            if tid is not None:  # pragma: no branch - tid is always found; defensive
                self._in_flight.pop(tid, None)
            try:
                results.append(task.result())
            except Exception as exc:
                _log.exception("backend raised for task %s", tid)
                if tid is not None:  # pragma: no branch - tid is always found; defensive
                    from claude_runner.models import TokenUsage

                    results.append(
                        DispatchResult(
                            task_id=tid,
                            success=False,
                            usage=TokenUsage(),
                            stop_reason=StopReason.ERROR,
                            session_id=None,
                            duration_s=0.0,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    )
        return results

    async def _sleep_until(self, when: datetime) -> None:
        now = datetime.now(tz=UTC)
        secs = max((when - now).total_seconds(), 1.0)
        self._emitter.emit("sleeping", seconds=int(secs), until=when.isoformat())
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._await_any_soft(), timeout=secs)

    async def _await_any_soft(self) -> None:
        if not self._in_flight:
            # Block long-ish; outer wait_for will cancel this.
            await asyncio.sleep(3600)
            return  # pragma: no cover - unreachable; sleep(3600) is always cancelled first
        await asyncio.wait(list(self._in_flight.values()), return_when=asyncio.FIRST_COMPLETED)

    async def _drain(self) -> None:
        if not self._in_flight:
            return
        _log.info("draining %d in-flight task(s)", len(self._in_flight))
        await asyncio.gather(*self._in_flight.values(), return_exceptions=True)
        self._in_flight.clear()
