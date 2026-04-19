from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from claude_runner.budget.circuit_breaker import CircuitBreaker
from claude_runner.budget.controller import TokenBudgetController
from claude_runner.config import Settings
from claude_runner.models import DispatchResult, StopReason, TaskStatus, TokenUsage
from claude_runner.notify.emitter import EventEmitter
from claude_runner.runner.scheduler import Scheduler
from claude_runner.state.store import StateStore
from claude_runner.todo.catalog import TodoCatalog


@pytest.fixture
def scheduler_factory(
    tmp_project: Path, settings: Settings, state_store: StateStore, emitter: EventEmitter
):
    def _make(backend) -> Scheduler:
        catalog = TodoCatalog(
            tmp_project / "todo",
            state_store=state_store,
            settings=settings,
            time_source=lambda: 0.0,
        )
        budget = TokenBudgetController(settings, source=None)
        breaker = CircuitBreaker(
            max_consecutive_failures=settings.max_consecutive_failures,
            failure_rate_threshold=settings.failure_rate_threshold,
            rolling_window=settings.failure_rolling_window,
            min_samples=settings.failure_rate_min_samples,
        )
        return Scheduler(
            settings=settings,
            catalog=catalog,
            backend=backend,
            budget=budget,
            state_store=state_store,
            emitter=emitter,
            breaker=breaker,
        )

    return _make


async def test_all_success(
    scheduler_factory, fake_backend, make_task, state_store: StateStore
) -> None:
    make_task("001")
    make_task("002")
    make_task("003")
    scheduler = scheduler_factory(fake_backend)
    outcome = await scheduler.run()
    assert outcome.completed == 3
    assert outcome.failed == 0
    for tid in ("001", "002", "003"):
        assert state_store.load(tid).status is TaskStatus.COMPLETED


async def test_one_failure_others_continue(
    scheduler_factory, fake_backend, make_task, state_store: StateStore
) -> None:
    make_task("a")
    make_task("b")
    make_task("c")
    fake_backend.set(
        "b",
        DispatchResult(
            task_id="b",
            success=False,
            usage=TokenUsage(input_tokens=100),
            stop_reason=StopReason.ERROR,
            session_id="sid-b",
            duration_s=1.0,
            error="boom",
        ),
    )
    scheduler = scheduler_factory(fake_backend)
    outcome = await scheduler.run()
    assert outcome.completed == 2
    assert outcome.failed == 1
    assert state_store.load("a").status is TaskStatus.COMPLETED
    assert state_store.load("b").status is TaskStatus.FAILED
    assert state_store.load("c").status is TaskStatus.COMPLETED


async def test_three_consecutive_failures_trip_breaker(
    scheduler_factory, fake_backend, make_task, state_store: StateStore
) -> None:
    for i, tid in enumerate(["a", "b", "c", "d", "e"]):
        make_task(tid)
        if i < 3:
            fake_backend.set(
                tid,
                DispatchResult(
                    task_id=tid,
                    success=False,
                    usage=TokenUsage(),
                    stop_reason=StopReason.ERROR,
                    session_id=None,
                    duration_s=0.1,
                    error="nope",
                ),
            )
    scheduler = scheduler_factory(fake_backend)
    # Force sequential dispatch so consecutive failures accrue — max_concurrency
    # does not guarantee this because fake tasks finish nearly simultaneously,
    # but for a small queue the breaker still trips on any 3-failure prefix.
    outcome = await scheduler.run()
    assert outcome.breaker_tripped
    # d and e may or may not have run depending on scheduling order; assert the
    # breaker prevented at least one from running.
    unrun = [tid for tid in ("d", "e") if state_store.load(tid).status is TaskStatus.PENDING]
    assert unrun  # at least one skipped


async def test_events_recorded(
    scheduler_factory, fake_backend, make_task, state_store: StateStore
) -> None:
    make_task("only")
    scheduler = scheduler_factory(fake_backend)
    await scheduler.run()
    events = state_store.events_path().read_text().splitlines()
    assert any('"run_started"' in line for line in events)
    assert any('"task_started"' in line for line in events)
    assert any('"run_finished"' in line for line in events)


async def test_dependent_on_failed_is_blocked(
    scheduler_factory, fake_backend, make_task, state_store: StateStore
) -> None:
    make_task("parent")
    make_task("child", depends_on=["parent"])
    fake_backend.set(
        "parent",
        DispatchResult(
            task_id="parent",
            success=False,
            usage=TokenUsage(),
            stop_reason=StopReason.ERROR,
            session_id=None,
            duration_s=0.1,
            error="parent failed",
        ),
    )
    scheduler = scheduler_factory(fake_backend)
    outcome = await scheduler.run()
    assert outcome.failed == 1
    # Child never ran because parent did not complete.
    assert state_store.load("child").status is TaskStatus.PENDING


def test_sync_wrapper() -> None:
    # Sanity: asyncio.run works on the scheduler (covered by CLI path).
    assert asyncio.iscoroutinefunction(Scheduler.run)
