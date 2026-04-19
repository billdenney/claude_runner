"""Direct-call tests for Scheduler helpers that are hard to hit through the
main loop. These give us coverage of early-return and drain branches
without having to engineer an elaborate scheduling scenario."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from claude_runner.budget.circuit_breaker import CircuitBreaker
from claude_runner.budget.controller import (
    Decision,
    DecisionKind,
    TokenBudgetController,
)
from claude_runner.config import Settings
from claude_runner.notify.emitter import EventEmitter
from claude_runner.runner.scheduler import Scheduler
from claude_runner.state.store import StateStore
from claude_runner.todo.catalog import TodoCatalog


def _make_scheduler(
    tmp_project: Path,
    settings: Settings,
    state_store: StateStore,
    emitter: EventEmitter,
    backend,
    budget: TokenBudgetController | None = None,
) -> Scheduler:
    catalog = TodoCatalog(
        tmp_project / "todo",
        state_store=state_store,
        settings=settings,
        time_source=lambda: 0.0,
    )
    return Scheduler(
        settings=settings,
        catalog=catalog,
        backend=backend,
        budget=budget or TokenBudgetController(settings, source=None),
        state_store=state_store,
        emitter=emitter,
        breaker=CircuitBreaker(
            max_consecutive_failures=3,
            failure_rate_threshold=0.5,
            rolling_window=10,
            min_samples=4,
        ),
    )


@pytest.mark.asyncio
async def test_await_any_returns_empty_list_when_no_in_flight(
    tmp_project: Path,
    settings: Settings,
    state_store: StateStore,
    emitter: EventEmitter,
    fake_backend,
) -> None:
    scheduler = _make_scheduler(tmp_project, settings, state_store, emitter, fake_backend)
    assert await scheduler._await_any() == []


@pytest.mark.asyncio
async def test_drain_awaits_any_remaining_in_flight_tasks(
    tmp_project: Path,
    settings: Settings,
    state_store: StateStore,
    emitter: EventEmitter,
    fake_backend,
) -> None:
    """_drain should await outstanding asyncio tasks and clear the dict, even
    when those tasks have already finished."""
    scheduler = _make_scheduler(tmp_project, settings, state_store, emitter, fake_backend)

    async def _nop() -> None:
        return None

    task = asyncio.create_task(_nop())
    scheduler._in_flight["tid"] = task  # type: ignore[assignment]
    await scheduler._drain()
    assert scheduler._in_flight == {}


@pytest.mark.asyncio
async def test_drain_is_noop_when_in_flight_empty(
    tmp_project: Path,
    settings: Settings,
    state_store: StateStore,
    emitter: EventEmitter,
    fake_backend,
) -> None:
    scheduler = _make_scheduler(tmp_project, settings, state_store, emitter, fake_backend)
    await scheduler._drain()  # should not raise


@pytest.mark.asyncio
async def test_await_any_soft_waits_on_existing_tasks(
    tmp_project: Path,
    settings: Settings,
    state_store: StateStore,
    emitter: EventEmitter,
    fake_backend,
) -> None:
    """When _await_any_soft is invoked with in-flight tasks present (the
    non-sleep branch), it returns as soon as one completes."""
    scheduler = _make_scheduler(tmp_project, settings, state_store, emitter, fake_backend)

    async def _fast() -> None:
        await asyncio.sleep(0)

    task = asyncio.create_task(_fast())
    scheduler._in_flight["x"] = task  # type: ignore[assignment]
    await scheduler._await_any_soft()
    # Task finished; cleanup via drain.
    await scheduler._drain()


@pytest.mark.asyncio
async def test_scheduler_breaks_when_no_slots_no_in_flight_no_wait(
    tmp_project: Path,
    settings: Settings,
    state_store: StateStore,
    emitter: EventEmitter,
    fake_backend,
    make_task,
) -> None:
    """Rare case: catalog has ready tasks, but target_concurrency collapses to
    zero and nothing is in flight → scheduler bails out cleanly."""

    class ZeroConcurrencyBudget(TokenBudgetController):
        def target_concurrency(self) -> int:  # type: ignore[override]
            return 0

        def may_start(self, task_estimate: int) -> Decision:  # type: ignore[override]
            return Decision(kind=DecisionKind.OK)

    make_task("solo")
    budget = ZeroConcurrencyBudget(settings, source=None)
    scheduler = _make_scheduler(
        tmp_project, settings, state_store, emitter, fake_backend, budget=budget
    )
    # Should terminate (not livelock) with completed=0.
    outcome = await asyncio.wait_for(scheduler.run(), timeout=2.0)
    assert outcome.completed == 0
    assert outcome.failed == 0
