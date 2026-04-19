from __future__ import annotations

from datetime import UTC, datetime
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


class ResumeAwareBackend:
    """Records the session_id the scheduler passes in so we can assert on it."""

    name = "resume-aware"

    def __init__(self, *, state_store: StateStore) -> None:
        self._state = state_store
        self.seen_sessions: list[str | None] = []

    async def run_task(self, spec):
        state = self._state.load(spec.id)
        self.seen_sessions.append(state.session_id)
        state.status = TaskStatus.RUNNING
        if state.session_id is None:
            state.session_id = f"sid-fresh-{spec.id}"
        self._state.save(state)

        result = DispatchResult(
            task_id=spec.id,
            success=True,
            usage=TokenUsage(input_tokens=10, output_tokens=5),
            stop_reason=StopReason.END_TURN,
            session_id=state.session_id,
            duration_s=0.1,
        )
        state.status = TaskStatus.COMPLETED
        state.stop_reason = StopReason.END_TURN
        state.last_finished_at = datetime.now(tz=UTC)
        self._state.save(state)
        return result


@pytest.mark.asyncio
async def test_interrupted_task_resumes_with_session_id(
    tmp_project: Path, settings: Settings, state_store: StateStore, emitter: EventEmitter, make_task
) -> None:
    make_task("alpha")
    # Simulate an earlier interrupted run that captured a session id.
    pre = state_store.load("alpha")
    pre.status = TaskStatus.INTERRUPTED
    pre.session_id = "sid-saved-alpha"
    pre.attempts = 1
    state_store.save(pre)

    backend = ResumeAwareBackend(state_store=state_store)
    catalog = TodoCatalog(
        tmp_project / "todo", state_store=state_store, settings=settings, time_source=lambda: 0.0
    )
    budget = TokenBudgetController(settings, source=None)
    breaker = CircuitBreaker(
        max_consecutive_failures=settings.max_consecutive_failures,
        failure_rate_threshold=settings.failure_rate_threshold,
        rolling_window=settings.failure_rolling_window,
        min_samples=settings.failure_rate_min_samples,
    )
    scheduler = Scheduler(
        settings=settings,
        catalog=catalog,
        backend=backend,
        budget=budget,
        state_store=state_store,
        emitter=emitter,
        breaker=breaker,
    )
    outcome = await scheduler.run()
    assert outcome.completed == 1
    # The backend should have seen the preserved session id on its first look.
    assert backend.seen_sessions[0] == "sid-saved-alpha"
    final = state_store.load("alpha")
    assert final.status is TaskStatus.COMPLETED
    assert final.session_id == "sid-saved-alpha"
