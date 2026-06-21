"""Tests for the pre-dispatch-hook DEFERRAL path (`_record_pre_dispatch_deferral`).

The pre-dispatch hook documents an exit-code contract (ADR-0013 / the
popPK queue's ``setup_worktree.sh``):

* ``exit 1``          -> TRANSIENT defer (an input paper awaiting operator
                         re-acquisition, or a pending ``*_trimmed.md``).
                         NOT a task failure.
* other non-zero      -> HARD failure (config/git bug); operator triages.

Before this path existed, the dispatcher recorded *every* non-zero hook
exit as ``pre_dispatch_hook_failed`` and counted it toward the circuit
breaker — so a paper merely awaiting re-acquisition burned through
``failure_circuit_breaker_threshold`` deferrals and died as
``failed_circuit_breaker`` (observed live on the popPK queue: 5 tasks,
June 2026, e.g. ``zotero-009`` awaiting ``PMID_22257150``).

The fix honors the contract: an ``exit 1`` deferral parks the task in the
``deferred`` status with a re-check cooldown and is deliberately kept out
of ``runs`` so it can never reach the breaker counter. This file covers
``_record_pre_dispatch_deferral`` in isolation; the dispatch-level
routing (exit 1 -> deferral vs. exit 2 -> failure) is covered in
``tests/integration/test_dispatcher.py``; the orchestrator's cooldown
gating in ``tests/unit/test_orchestrator_sidecar_resume.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from claude_task_runner.config.schema import FailureClassifierSettings
from claude_task_runner.queue.schema import RunRecord, Task, TaskState, TokenUsage
from claude_task_runner.runner.dispatcher import _record_pre_dispatch_deferral
from claude_task_runner.runner.hooks import HookResult
from claude_task_runner.runner.session import ResumeStrategy, SpawnPlan


@pytest.fixture
def when() -> datetime:
    return datetime(2026, 6, 21, 12, 0, 0, tzinfo=UTC)


class _StaticClock:
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
def deferral_hook_result() -> HookResult:
    """A clean exit-1 deferral, mirroring the live re-acquisition defer."""
    return HookResult(
        command="/path/to/setup_worktree.sh",
        exit_code=1,
        stdout="",
        stderr=(
            "DEFERRED: zotero-009-bai_2012_unknown — input awaits "
            "re-acquisition: /q/papers/PMID_22257150/PMID_22257150.pdf"
        ),
        duration_s=0.002,
        timed_out=False,
    )


@pytest.fixture
def settings_threshold_5() -> FailureClassifierSettings:
    return FailureClassifierSettings(
        environmental_patterns=[],
        operator_patterns=[],
        task_patterns=[],
        failure_circuit_breaker_threshold=5,
    )


def _make_task(task_id: str = "t1") -> Task:
    return Task(id=task_id, title=f"test task {task_id}", prompt="do the thing")


def _deferral_run(*, attempt: int, when: datetime) -> RunRecord:
    """A *failure* RunRecord (what the OLD code wrongly appended for a
    deferral). Used to prove that even with such history present the
    deferral path no longer grows ``runs`` or trips the breaker."""
    return RunRecord(
        attempt=attempt,
        started_at=when,
        finished_at=when,
        stop_reason="pre_dispatch_hook_failed",
        error="pre-dispatch hook exited 1: DEFERRED ...",
        duration_s=0.0,
        usage=TokenUsage(),
    )


class TestPreDispatchHookDeferral:
    def test_exit1_parks_in_deferred_status(
        self,
        when: datetime,
        static_clock: _StaticClock,
        fresh_plan: SpawnPlan,
        deferral_hook_result: HookResult,
        settings_threshold_5: FailureClassifierSettings,
        tmp_path: Path,
    ) -> None:
        """A clean exit-1 deferral parks the task in ``deferred`` — NOT
        ``failed`` / ``failed_circuit_breaker``."""
        task = _make_task()
        prior = TaskState(task_id=task.id, status="pending", attempts=0, runs=[])
        outcome = _record_pre_dispatch_deferral(
            task=task,
            state=prior,
            plan=fresh_plan,
            queue_dir=tmp_path,
            hook_result=deferral_hook_result,
            clock=static_clock,
            persist_state=False,
            settings_failure_classifier=settings_threshold_5,
        )
        assert outcome.new_state.status == "deferred"
        assert outcome.new_state.deferral_count == 1
        assert outcome.new_state.deferred_reason is not None
        assert "re-acquisition" in outcome.new_state.deferred_reason

    def test_deferral_does_not_count_as_attempt_or_run(
        self,
        when: datetime,
        static_clock: _StaticClock,
        fresh_plan: SpawnPlan,
        deferral_hook_result: HookResult,
        settings_threshold_5: FailureClassifierSettings,
        tmp_path: Path,
    ) -> None:
        """The breaker counts trailing *failure runs*; a deferral must not
        append one, and must not consume an ``attempts`` slot."""
        task = _make_task()
        prior = TaskState(task_id=task.id, status="pending", attempts=3, runs=[])
        outcome = _record_pre_dispatch_deferral(
            task=task,
            state=prior,
            plan=fresh_plan,
            queue_dir=tmp_path,
            hook_result=deferral_hook_result,
            clock=static_clock,
            persist_state=False,
            settings_failure_classifier=settings_threshold_5,
        )
        assert outcome.new_state.runs == []
        assert outcome.new_state.attempts == 3  # unchanged

    def test_many_deferrals_never_trip_circuit_breaker(
        self,
        when: datetime,
        static_clock: _StaticClock,
        fresh_plan: SpawnPlan,
        deferral_hook_result: HookResult,
        settings_threshold_5: FailureClassifierSettings,
        tmp_path: Path,
    ) -> None:
        """The live regression: a task awaiting re-acquisition deferred 70
        times. With threshold=5 the OLD path was ``failed_circuit_breaker``
        by attempt 5; the deferral path keeps it ``deferred`` forever (it
        re-checks until the file arrives or an operator removes it)."""
        task = _make_task()
        prior = TaskState(
            task_id=task.id,
            status="deferred",
            attempts=0,
            deferral_count=70,
            runs=[],  # deferrals never appended runs
        )
        outcome = _record_pre_dispatch_deferral(
            task=task,
            state=prior,
            plan=fresh_plan,
            queue_dir=tmp_path,
            hook_result=deferral_hook_result,
            clock=static_clock,
            persist_state=False,
            settings_failure_classifier=settings_threshold_5,
        )
        assert outcome.new_state.status == "deferred"
        assert outcome.new_state.deferral_count == 71

    def test_deferral_sets_cooldown_from_settings(
        self,
        when: datetime,
        static_clock: _StaticClock,
        fresh_plan: SpawnPlan,
        deferral_hook_result: HookResult,
        tmp_path: Path,
    ) -> None:
        """``next_eligible_at`` = now + configured cooldown — the
        orchestrator skips the parked task until then."""
        settings = FailureClassifierSettings(
            environmental_patterns=[],
            operator_patterns=[],
            task_patterns=[],
            failure_circuit_breaker_threshold=3,
            deferral_recheck_cooldown_s=60.0,
        )
        task = _make_task()
        prior = TaskState(task_id=task.id, status="pending", attempts=0, runs=[])
        outcome = _record_pre_dispatch_deferral(
            task=task,
            state=prior,
            plan=fresh_plan,
            queue_dir=tmp_path,
            hook_result=deferral_hook_result,
            clock=static_clock,
            persist_state=False,
            settings_failure_classifier=settings,
        )
        assert outcome.new_state.next_eligible_at == when + timedelta(seconds=60.0)

    def test_deferral_default_cooldown_without_settings(
        self,
        when: datetime,
        static_clock: _StaticClock,
        fresh_plan: SpawnPlan,
        deferral_hook_result: HookResult,
        tmp_path: Path,
    ) -> None:
        """No classifier settings (legacy callers): falls back to the
        15-minute default cooldown rather than crashing."""
        task = _make_task()
        prior = TaskState(task_id=task.id, status="pending", attempts=0, runs=[])
        outcome = _record_pre_dispatch_deferral(
            task=task,
            state=prior,
            plan=fresh_plan,
            queue_dir=tmp_path,
            hook_result=deferral_hook_result,
            clock=static_clock,
            persist_state=False,
            settings_failure_classifier=None,
        )
        assert outcome.new_state.next_eligible_at == when + timedelta(seconds=900.0)

    def test_deferral_run_record_for_log_only(
        self,
        when: datetime,
        static_clock: _StaticClock,
        fresh_plan: SpawnPlan,
        deferral_hook_result: HookResult,
        settings_threshold_5: FailureClassifierSettings,
        tmp_path: Path,
    ) -> None:
        """The returned run_record carries the deferral stop_reason for the
        attempt log/stream, even though it is NOT appended to runs."""
        task = _make_task()
        prior = TaskState(task_id=task.id, status="pending", attempts=0, runs=[])
        outcome = _record_pre_dispatch_deferral(
            task=task,
            state=prior,
            plan=fresh_plan,
            queue_dir=tmp_path,
            hook_result=deferral_hook_result,
            clock=static_clock,
            persist_state=False,
            settings_failure_classifier=settings_threshold_5,
        )
        assert outcome.run_record.stop_reason == "pre_dispatch_deferred"
        assert outcome.new_state.stop_reason == "pre_dispatch_deferred"
        # run_record is NOT in the persisted runs list (breaker accounting).
        assert outcome.run_record not in outcome.new_state.runs

    def test_deferral_persists_state_when_requested(
        self,
        when: datetime,
        static_clock: _StaticClock,
        fresh_plan: SpawnPlan,
        deferral_hook_result: HookResult,
        settings_threshold_5: FailureClassifierSettings,
        tmp_path: Path,
    ) -> None:
        """With persist_state=True the deferred state is written atomically
        and round-trips (including the new fields)."""
        from claude_task_runner.queue.store import load_state, state_path_for

        task = _make_task()
        prior = TaskState(task_id=task.id, status="pending", attempts=0, runs=[])
        outcome = _record_pre_dispatch_deferral(
            task=task,
            state=prior,
            plan=fresh_plan,
            queue_dir=tmp_path,
            hook_result=deferral_hook_result,
            clock=static_clock,
            persist_state=True,
            settings_failure_classifier=settings_threshold_5,
        )
        loaded = load_state(state_path_for(tmp_path, task.id))
        assert loaded == outcome.new_state
        assert loaded.status == "deferred"
        assert loaded.next_eligible_at == when + timedelta(seconds=900.0)
