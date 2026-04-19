from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from claude_runner.budget.circuit_breaker import CircuitBreaker
from claude_runner.budget.controller import Decision, DecisionKind, TokenBudgetController
from claude_runner.config import Settings
from claude_runner.notify.emitter import EventEmitter
from claude_runner.runner.scheduler import Scheduler
from claude_runner.state.store import StateStore
from claude_runner.todo.catalog import TodoCatalog


class StopController(TokenBudgetController):
    """Budget controller that immediately returns STOP on may_start."""

    def may_start(self, task_estimate: int) -> Decision:  # type: ignore[override]
        return Decision(kind=DecisionKind.STOP, reason="test STOP")


class OneWaitThenOkController(TokenBudgetController):
    """Returns WAIT once, then OK thereafter — to exercise the sleep path."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._waited = False

    def may_start(self, task_estimate: int) -> Decision:  # type: ignore[override]
        if not self._waited:
            self._waited = True
            return Decision(
                kind=DecisionKind.WAIT,
                wait_until=datetime.now(tz=UTC) + timedelta(seconds=0.05),
                reason="test WAIT",
            )
        return Decision(kind=DecisionKind.OK)


@pytest.mark.asyncio
async def test_stop_decision_halts_scheduler(
    tmp_project: Path,
    settings: Settings,
    state_store: StateStore,
    emitter: EventEmitter,
    fake_backend,
    make_task,
) -> None:
    make_task("a")
    catalog = TodoCatalog(
        tmp_project / "todo", state_store=state_store, settings=settings, time_source=lambda: 0.0
    )
    budget = StopController(settings, source=None)
    breaker = CircuitBreaker(
        max_consecutive_failures=3,
        failure_rate_threshold=0.5,
        rolling_window=10,
        min_samples=4,
    )
    scheduler = Scheduler(
        settings=settings,
        catalog=catalog,
        backend=fake_backend,
        budget=budget,
        state_store=state_store,
        emitter=emitter,
        breaker=breaker,
    )
    outcome = await scheduler.run()
    assert outcome.completed == 0
    assert outcome.failed == 0
    assert not outcome.breaker_tripped


@pytest.mark.asyncio
async def test_wait_then_ok_sleeps_and_runs(
    tmp_project: Path,
    settings: Settings,
    state_store: StateStore,
    emitter: EventEmitter,
    fake_backend,
    make_task,
) -> None:
    make_task("a")
    catalog = TodoCatalog(
        tmp_project / "todo", state_store=state_store, settings=settings, time_source=lambda: 0.0
    )
    budget = OneWaitThenOkController(settings, source=None)
    breaker = CircuitBreaker(
        max_consecutive_failures=3,
        failure_rate_threshold=0.5,
        rolling_window=10,
        min_samples=4,
    )
    scheduler = Scheduler(
        settings=settings,
        catalog=catalog,
        backend=fake_backend,
        budget=budget,
        state_store=state_store,
        emitter=emitter,
        breaker=breaker,
    )
    outcome = await asyncio.wait_for(scheduler.run(), timeout=5.0)
    assert outcome.completed == 1
