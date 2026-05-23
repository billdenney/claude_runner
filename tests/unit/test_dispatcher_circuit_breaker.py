"""Regression tests for the failure-circuit-breaker wiring in _finalize_state.

`_finalize_state` is a pure function on TaskState + RunRecord + Settings, so
we can exercise it directly without spawning subprocesses. These tests
cover the bug we hit live during dispatch bring-up: a task whose agent
exits with `stop_sequence` is classified `failed` -> stays in
`_DISPATCHABLE_STATUSES` -> gets re-dispatched indefinitely. The
threshold-gated transition to `failed_circuit_breaker` is what breaks
that loop.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from claude_task_runner.config.schema import FailureClassifierSettings
from claude_task_runner.queue.schema import (
    RunRecord,
    TaskState,
    TokenUsage,
)
from claude_task_runner.runner.dispatcher import (
    _count_trailing_failures,
    _finalize_state,
)
from claude_task_runner.runner.session import ResumeStrategy, SpawnPlan
from claude_task_runner.runner.stream import StreamSummary


@pytest.fixture
def when() -> datetime:
    return datetime(2026, 5, 14, 21, 0, 0, tzinfo=UTC)


@pytest.fixture
def settings_threshold_3() -> FailureClassifierSettings:
    return FailureClassifierSettings(
        environmental_patterns=[],
        operator_patterns=[],
        task_patterns=[],
        failure_circuit_breaker_threshold=3,
    )


@pytest.fixture
def fresh_plan() -> SpawnPlan:
    return SpawnPlan(
        strategy=ResumeStrategy.FRESH,
        session_id=None,
        prompt="test prompt",
        extra_args=[],
    )


def _failed_run(
    *, attempt: int, when: datetime, stop_reason: str = "max_tokens", error: str | None = None
) -> RunRecord:
    # PR 16: default failure stop_reason changed from "stop_sequence" to
    # "max_tokens". stop_sequence is now treated as a clean API-level
    # completion (alongside end_turn / result), so the prior default
    # would silently classify these "failed_run" fixtures as successes.
    # max_tokens is a stable cap-violation stop_reason that's never been
    # in the success set.
    return RunRecord(
        attempt=attempt,
        started_at=when,
        finished_at=when,
        stop_reason=stop_reason,
        error=error,
        duration_s=2.4,
        usage=TokenUsage(),
    )


def _successful_run(*, attempt: int, when: datetime) -> RunRecord:
    return RunRecord(
        attempt=attempt,
        started_at=when,
        finished_at=when,
        stop_reason="end_turn",
        error=None,
        duration_s=2.4,
        usage=TokenUsage(),
    )


class TestCountTrailingFailures:
    def test_empty_runs(self, when: datetime) -> None:
        assert _count_trailing_failures([]) == 0

    def test_single_failure(self, when: datetime) -> None:
        assert _count_trailing_failures([_failed_run(attempt=1, when=when)]) == 1

    def test_failure_after_success_resets(self, when: datetime) -> None:
        runs = [
            _failed_run(attempt=1, when=when),
            _successful_run(attempt=2, when=when),
            _failed_run(attempt=3, when=when),
        ]
        # Only the last failure counts; the success before it broke the streak.
        assert _count_trailing_failures(runs) == 1

    def test_stop_sequence_counts_as_success(self, when: datetime) -> None:
        """PR 16: ``stop_sequence`` is recognised as a clean API-level
        completion (alongside ``end_turn`` / ``result``). Before PR 16
        this counted as a failure, which produced the chen_2016 /
        garonzik_2016 / li_2015 false-positive failed-classifications
        observed live on 2026-05-22/23 — each task had pushed its
        commit, written its report, and exited cleanly via
        stop_sequence."""
        runs = [_failed_run(attempt=1, when=when, stop_reason="stop_sequence")]
        # No trailing failures: stop_sequence breaks the failure streak
        # the same way end_turn does.
        assert _count_trailing_failures(runs) == 0

    def test_max_tokens_still_counts_as_failure(self, when: datetime) -> None:
        """``max_tokens`` (cap hit mid-output) remains a failure — only
        the clean API-level "I'm done" stop reasons are successes."""
        runs = [_failed_run(attempt=1, when=when, stop_reason="max_tokens")]
        assert _count_trailing_failures(runs) == 1

    def test_tool_use_still_counts_as_failure(self, when: datetime) -> None:
        """``tool_use`` means the model handed control back to the
        caller mid-conversation without resolving — not a completion,
        so should not break the failure streak."""
        runs = [_failed_run(attempt=1, when=when, stop_reason="tool_use")]
        assert _count_trailing_failures(runs) == 1


class TestFinalizeStateCircuitBreaker:
    def test_first_failure_is_plain_failed(
        self,
        when: datetime,
        settings_threshold_3: FailureClassifierSettings,
        fresh_plan: SpawnPlan,
    ) -> None:
        prior = TaskState(task_id="t1", status="pending", attempts=0, runs=[])
        run = _failed_run(attempt=1, when=when)
        new, _ = _finalize_state(
            prior=prior,
            plan=fresh_plan,
            run=run,
            summary=StreamSummary(),
            cap_violation=None,
            settings_failure_classifier=settings_threshold_3,
        )
        assert new.status == "failed"

    def test_threshold_trips_to_circuit_breaker(
        self,
        when: datetime,
        settings_threshold_3: FailureClassifierSettings,
        fresh_plan: SpawnPlan,
    ) -> None:
        # Two trailing failures already; this attempt is the 3rd.
        prior_runs = [
            _failed_run(attempt=1, when=when),
            _failed_run(attempt=2, when=when),
        ]
        prior = TaskState(
            task_id="t1",
            status="failed",
            attempts=2,
            runs=prior_runs,
        )
        third_run = _failed_run(attempt=3, when=when)
        new, _ = _finalize_state(
            prior=prior,
            plan=fresh_plan,
            run=third_run,
            summary=StreamSummary(),
            cap_violation=None,
            settings_failure_classifier=settings_threshold_3,
        )
        assert new.status == "failed_circuit_breaker"

    def test_success_clears_the_streak(
        self,
        when: datetime,
        settings_threshold_3: FailureClassifierSettings,
        fresh_plan: SpawnPlan,
    ) -> None:
        prior_runs = [
            _failed_run(attempt=1, when=when),
            _failed_run(attempt=2, when=when),
            _successful_run(attempt=3, when=when),  # streak broken
            _failed_run(attempt=4, when=when),
        ]
        prior = TaskState(
            task_id="t1",
            status="failed",
            attempts=4,
            runs=prior_runs,
        )
        # Now we add a 5th run that also fails. Only the 4th and 5th are
        # trailing failures -> count=2 -> below threshold of 3 -> plain failed.
        fifth_run = _failed_run(attempt=5, when=when)
        new, _ = _finalize_state(
            prior=prior,
            plan=fresh_plan,
            run=fifth_run,
            summary=StreamSummary(),
            cap_violation=None,
            settings_failure_classifier=settings_threshold_3,
        )
        assert new.status == "failed"

    def test_completion_never_trips(
        self,
        when: datetime,
        settings_threshold_3: FailureClassifierSettings,
        fresh_plan: SpawnPlan,
    ) -> None:
        prior_runs = [_failed_run(attempt=i, when=when) for i in range(1, 100)]
        prior = TaskState(
            task_id="t1",
            status="failed",
            attempts=99,
            runs=prior_runs,
        )
        # The 100th attempt finally succeeds. Even after 99 failures the
        # final status is completed, not circuit-broken.
        success = _successful_run(attempt=100, when=when)
        new, _ = _finalize_state(
            prior=prior,
            plan=fresh_plan,
            run=success,
            summary=StreamSummary(),
            cap_violation=None,
            settings_failure_classifier=settings_threshold_3,
        )
        assert new.status == "completed"

    def test_no_classifier_settings_keeps_legacy_behavior(
        self,
        when: datetime,
        fresh_plan: SpawnPlan,
    ) -> None:
        # When settings_failure_classifier is None (callers that haven't
        # been threaded yet), behaviour matches pre-fix: just "failed",
        # never circuit-broken.
        prior_runs = [_failed_run(attempt=i, when=when) for i in range(1, 6)]
        prior = TaskState(
            task_id="t1",
            status="failed",
            attempts=5,
            runs=prior_runs,
        )
        sixth_run = _failed_run(attempt=6, when=when)
        new, _ = _finalize_state(
            prior=prior,
            plan=fresh_plan,
            run=sixth_run,
            summary=StreamSummary(),
            cap_violation=None,
            settings_failure_classifier=None,
        )
        assert new.status == "failed"
