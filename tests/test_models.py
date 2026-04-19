"""Coverage for the small helpers on TaskState and RunRecord."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from claude_runner.models import (
    RunRecord,
    StopReason,
    TaskState,
    TaskStatus,
    TokenUsage,
)


def test_is_terminal_covers_all_finished_statuses() -> None:
    for s in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.BLOCKED):
        assert TaskState(task_id="x", status=s).is_terminal()
    for s in (
        TaskStatus.PENDING,
        TaskStatus.QUEUED,
        TaskStatus.RUNNING,
        TaskStatus.INTERRUPTED,
    ):
        assert not TaskState(task_id="x", status=s).is_terminal()


def test_is_in_flight_true_for_running_and_queued() -> None:
    assert TaskState(task_id="x", status=TaskStatus.RUNNING).is_in_flight()
    assert TaskState(task_id="x", status=TaskStatus.QUEUED).is_in_flight()
    assert not TaskState(task_id="x", status=TaskStatus.PENDING).is_in_flight()


def test_needs_resume_requires_interrupted_plus_session() -> None:
    # Interrupted + session_id -> needs resume
    assert TaskState(task_id="x", status=TaskStatus.INTERRUPTED, session_id="sid").needs_resume()
    # Running + session_id -> also needs resume (process crashed mid-flight)
    assert TaskState(task_id="x", status=TaskStatus.RUNNING, session_id="sid").needs_resume()
    # No session_id -> no resume
    assert not TaskState(task_id="x", status=TaskStatus.INTERRUPTED).needs_resume()
    # Completed ignores session_id
    assert not TaskState(task_id="x", status=TaskStatus.COMPLETED, session_id="sid").needs_resume()


def test_run_record_duration_for_unfinished_run() -> None:
    """duration_s returns 0 when finished_at is None."""
    start = datetime(2026, 4, 19, 12, 0, tzinfo=UTC)
    r = RunRecord(attempt=1, started_at=start)
    assert r.duration_s == 0.0


def test_run_record_duration_positive_when_finished() -> None:
    start = datetime(2026, 4, 19, 12, 0, tzinfo=UTC)
    r = RunRecord(attempt=1, started_at=start, finished_at=start + timedelta(seconds=30))
    assert r.duration_s == 30.0


def test_token_usage_billable_total_excludes_cache_reads() -> None:
    u = TokenUsage(
        input_tokens=1_000,
        output_tokens=500,
        cache_read_tokens=600,
        cache_creation_tokens=100,
    )
    # uncached_input = 1000 - 600 - 100 = 300; billable = 300 + 500 + 100 = 900
    assert u.billable_total == 900
    assert u.uncached_input == 300


def test_token_usage_uncached_input_floors_at_zero() -> None:
    """If cache-read + cache-creation exceeds input, uncached_input is 0, not negative."""
    u = TokenUsage(input_tokens=100, cache_read_tokens=500, cache_creation_tokens=50)
    assert u.uncached_input == 0


def test_stop_reason_enum_has_string_values() -> None:
    assert StopReason.END_TURN.value == "end_turn"
    assert StopReason("error") is StopReason.ERROR
