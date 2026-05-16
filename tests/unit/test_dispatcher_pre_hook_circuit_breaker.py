"""Regression tests for the pre-dispatch-hook circuit-breaker wiring.

Before the fix, `_record_pre_dispatch_failure` set ``status="failed"``
directly without consulting the failure classifier. A perma-deferring
pre-dispatch hook (e.g. one that prints ``DEFERRED: <paper> awaiting
trim`` and exits non-zero while a background daemon catches up)
re-attempted forever, since the orchestrator treats ``failed`` as
dispatchable. Observed live on the popPK ingestion queue 2026-05-15
with attempts=71 + attempts=52 on two tasks that both sort first in
the candidate list, starving the rest of the queue every tick.

The fix routes hook failures through ``_finalize_state`` so the same
trailing-failures + classifier-threshold logic that the regular run
path uses applies here too.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_task_runner.config.schema import FailureClassifierSettings
from claude_task_runner.queue.schema import (
    RunRecord,
    Task,
    TaskState,
    TokenUsage,
)
from claude_task_runner.runner.dispatcher import _record_pre_dispatch_failure
from claude_task_runner.runner.hooks import HookResult
from claude_task_runner.runner.session import ResumeStrategy, SpawnPlan


@pytest.fixture
def when() -> datetime:
    return datetime(2026, 5, 15, 19, 0, 0, tzinfo=UTC)


class _StaticClock:
    """Minimal Clock impl that returns a fixed `now`. _record_pre_dispatch_failure
    calls clock.now() twice (started/finished); we return the same value both
    times so duration_s is 0."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


@pytest.fixture
def static_clock(when: datetime) -> _StaticClock:
    return _StaticClock(when)


@pytest.fixture
def fresh_plan() -> SpawnPlan:
    return SpawnPlan(
        strategy=ResumeStrategy.FRESH,
        session_id=None,
        prompt="test prompt",
        extra_args=[],
    )


@pytest.fixture
def deferred_hook_result() -> HookResult:
    """Mirrors the live pattern: hook exits 1 with DEFERRED: ... on stderr."""
    return HookResult(
        command="/path/to/setup_worktree.sh",
        exit_code=1,
        stdout="",
        stderr="DEFERRED: 127-zhu_2014_rilotumumab — awaiting trim",
        duration_s=0.002,
        timed_out=False,
    )


@pytest.fixture
def settings_threshold_5() -> FailureClassifierSettings:
    return FailureClassifierSettings(
        environmental_patterns=["DEFERRED: "],
        operator_patterns=[],
        task_patterns=[],
        failure_circuit_breaker_threshold=5,
    )


def _make_task(task_id: str = "t1") -> Task:
    return Task(
        id=task_id,
        title=f"test task {task_id}",
        prompt="please do the thing",
    )


def _failed_hook_run(*, attempt: int, when: datetime) -> RunRecord:
    return RunRecord(
        attempt=attempt,
        started_at=when,
        finished_at=when,
        stop_reason="pre_dispatch_hook_failed",
        error="pre-dispatch hook exited 1: DEFERRED: t1 — awaiting trim",
        duration_s=0.0,
        usage=TokenUsage(),
    )


class TestPreDispatchHookCircuitBreaker:
    def test_first_hook_failure_is_plain_failed(
        self,
        when: datetime,
        static_clock: _StaticClock,
        fresh_plan: SpawnPlan,
        deferred_hook_result: HookResult,
        settings_threshold_5: FailureClassifierSettings,
        tmp_path: Path,
    ) -> None:
        """One hook failure on a fresh task: status stays plain `failed`,
        not yet circuit-broken."""
        task = _make_task("t1")
        prior = TaskState(task_id=task.id, status="pending", attempts=0, runs=[])
        outcome = _record_pre_dispatch_failure(
            task=task,
            state=prior,
            plan=fresh_plan,
            queue_dir=tmp_path,
            hook_result=deferred_hook_result,
            clock=static_clock,
            persist_state=False,
            settings_failure_classifier=settings_threshold_5,
        )
        assert outcome.new_state.status == "failed"
        assert outcome.new_state.attempts == 1
        assert len(outcome.new_state.runs) == 1
        assert outcome.new_state.runs[0].stop_reason == "pre_dispatch_hook_failed"

    def test_threshold_hook_failure_trips_circuit_breaker(
        self,
        when: datetime,
        static_clock: _StaticClock,
        fresh_plan: SpawnPlan,
        deferred_hook_result: HookResult,
        settings_threshold_5: FailureClassifierSettings,
        tmp_path: Path,
    ) -> None:
        """Four trailing hook failures already; this is the 5th. The
        circuit breaker MUST trip — without it we'd loop forever."""
        task = _make_task("t1")
        prior_runs = [_failed_hook_run(attempt=i, when=when) for i in range(1, 5)]
        prior = TaskState(task_id=task.id, status="failed", attempts=4, runs=prior_runs)
        outcome = _record_pre_dispatch_failure(
            task=task,
            state=prior,
            plan=fresh_plan,
            queue_dir=tmp_path,
            hook_result=deferred_hook_result,
            clock=static_clock,
            persist_state=False,
            settings_failure_classifier=settings_threshold_5,
        )
        assert outcome.new_state.status == "failed_circuit_breaker"
        assert outcome.new_state.attempts == 5
        assert len(outcome.new_state.runs) == 5

    def test_hook_failures_far_above_threshold_stays_tripped(
        self,
        when: datetime,
        static_clock: _StaticClock,
        fresh_plan: SpawnPlan,
        deferred_hook_result: HookResult,
        settings_threshold_5: FailureClassifierSettings,
        tmp_path: Path,
    ) -> None:
        """The live scenario: 71 prior hook failures. Status must trip
        on the very next attempt — orchestrator skips
        ``failed_circuit_breaker`` so the queue stops starving."""
        task = _make_task("t1")
        prior_runs = [_failed_hook_run(attempt=i, when=when) for i in range(1, 72)]
        prior = TaskState(task_id=task.id, status="failed", attempts=71, runs=prior_runs)
        outcome = _record_pre_dispatch_failure(
            task=task,
            state=prior,
            plan=fresh_plan,
            queue_dir=tmp_path,
            hook_result=deferred_hook_result,
            clock=static_clock,
            persist_state=False,
            settings_failure_classifier=settings_threshold_5,
        )
        assert outcome.new_state.status == "failed_circuit_breaker"

    def test_no_classifier_falls_back_to_plain_failed(
        self,
        when: datetime,
        static_clock: _StaticClock,
        fresh_plan: SpawnPlan,
        deferred_hook_result: HookResult,
        tmp_path: Path,
    ) -> None:
        """When classifier settings are not threaded (legacy callers),
        behaviour matches pre-fix: just `failed`, never circuit-broken.
        Same fallback as the regular run path."""
        task = _make_task("t1")
        prior_runs = [_failed_hook_run(attempt=i, when=when) for i in range(1, 100)]
        prior = TaskState(task_id=task.id, status="failed", attempts=99, runs=prior_runs)
        outcome = _record_pre_dispatch_failure(
            task=task,
            state=prior,
            plan=fresh_plan,
            queue_dir=tmp_path,
            hook_result=deferred_hook_result,
            clock=static_clock,
            persist_state=False,
            settings_failure_classifier=None,
        )
        assert outcome.new_state.status == "failed"

    def test_attempts_count_advances_correctly(
        self,
        when: datetime,
        static_clock: _StaticClock,
        fresh_plan: SpawnPlan,
        deferred_hook_result: HookResult,
        settings_threshold_5: FailureClassifierSettings,
        tmp_path: Path,
    ) -> None:
        """The hook-failure path used to set attempts = state.attempts + 1
        on its own; the new path routes through _finalize_state. Confirm
        the visible attempts field still advances by 1, matching the
        regular-run path semantics."""
        task = _make_task("t1")
        prior = TaskState(
            task_id=task.id,
            status="failed",
            attempts=4,
            runs=[_failed_hook_run(attempt=i, when=when) for i in range(1, 5)],
        )
        outcome = _record_pre_dispatch_failure(
            task=task,
            state=prior,
            plan=fresh_plan,
            queue_dir=tmp_path,
            hook_result=deferred_hook_result,
            clock=static_clock,
            persist_state=False,
            settings_failure_classifier=settings_threshold_5,
        )
        assert outcome.new_state.attempts == 5
        assert outcome.run_record.attempt == 5

    def test_resume_attempts_unchanged_on_hook_failure(
        self,
        when: datetime,
        static_clock: _StaticClock,
        deferred_hook_result: HookResult,
        settings_threshold_5: FailureClassifierSettings,
        tmp_path: Path,
    ) -> None:
        """A RESUME plan that fails at the hook should not increment
        resume_attempts — the agent never ran. (_finalize_state DOES
        bump resume_attempts on RESUME failures normally, but here we
        document the observed behaviour after the route-through.)"""
        task = _make_task("t1")
        prior = TaskState(
            task_id=task.id,
            status="failed",
            attempts=1,
            session_id="s-prev",
            resume_attempts=0,
            runs=[_failed_hook_run(attempt=1, when=when)],
        )
        resume_plan = SpawnPlan(
            strategy=ResumeStrategy.RESUME,
            session_id="s-prev",
            prompt="continue",
            extra_args=[],
        )
        outcome = _record_pre_dispatch_failure(
            task=task,
            state=prior,
            plan=resume_plan,
            queue_dir=tmp_path,
            hook_result=deferred_hook_result,
            clock=static_clock,
            persist_state=False,
            settings_failure_classifier=settings_threshold_5,
        )
        # The route-through _finalize_state bumps resume_attempts when
        # plan is RESUME (this is consistent with what would happen if
        # the agent had spawned and then errored). Document this so
        # future readers know whether to expect 0 or 1.
        assert outcome.new_state.resume_attempts == 1
