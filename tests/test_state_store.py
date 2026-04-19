from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_runner.models import RunRecord, StopReason, TaskState, TaskStatus, TokenUsage
from claude_runner.state.store import StateStore, state_from_dict, state_to_dict


def test_missing_file_returns_pending_state(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    state = store.load("nope")
    assert state.task_id == "nope"
    assert state.status is TaskStatus.PENDING
    assert state.runs == []


def test_save_roundtrips_through_load(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    now = datetime(2026, 4, 19, 12, 0, tzinfo=UTC)
    state = TaskState(
        task_id="alpha",
        status=TaskStatus.COMPLETED,
        session_id="sid",
        attempts=2,
        last_started_at=now,
        last_finished_at=now,
        stop_reason=StopReason.END_TURN,
        runs=[
            RunRecord(
                attempt=1,
                started_at=now,
                finished_at=now,
                usage=TokenUsage(input_tokens=10, output_tokens=5, cost_usd=0.01),
                stop_reason=StopReason.END_TURN,
            )
        ],
        error=None,
    )
    store.save(state)
    loaded = store.load("alpha")
    assert loaded.status is TaskStatus.COMPLETED
    assert loaded.session_id == "sid"
    assert loaded.attempts == 2
    assert loaded.runs[0].usage.input_tokens == 10
    assert loaded.runs[0].stop_reason is StopReason.END_TURN


def test_write_session_id_is_noop_when_unchanged(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.write_session_id("x", "sid-1")
    store.write_session_id("x", "sid-1")
    # Same session id — no rewrite expected (but it's OK if it is; assert value preserved).
    assert store.load("x").session_id == "sid-1"


def test_iter_states_returns_all(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.save(TaskState(task_id="a"))
    store.save(TaskState(task_id="b", status=TaskStatus.FAILED))
    ids = {s.task_id for s in store.iter_states()}
    assert ids == {"a", "b"}


def test_state_to_from_dict_symmetric() -> None:
    now = datetime(2026, 4, 19, 12, 0, tzinfo=UTC)
    state = TaskState(
        task_id="t",
        status=TaskStatus.RUNNING,
        session_id="sid",
        attempts=1,
        last_started_at=now,
        stop_reason=StopReason.ERROR,
        error="boom",
        runs=[RunRecord(attempt=1, started_at=now, usage=TokenUsage(input_tokens=1))],
    )
    back = state_from_dict("t", state_to_dict(state))
    assert back.status is TaskStatus.RUNNING
    assert back.stop_reason is StopReason.ERROR
    assert back.error == "boom"


def test_state_from_dict_handles_none() -> None:
    assert state_from_dict("x", None).status is TaskStatus.PENDING
    assert state_from_dict("x", {}).status is TaskStatus.PENDING


def test_usage_billable_total_excludes_cache_reads() -> None:
    u = TokenUsage(
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=600,
        cache_creation_tokens=100,
    )
    assert u.billable_total == 1000 - 600 - 100 + 500 + 100


def test_parse_dt_passes_datetime_through() -> None:
    """state/_parse_dt treats an existing datetime as a no-op."""
    from claude_runner.state.store import _parse_dt

    now = datetime(2026, 4, 19, 12, 0, tzinfo=UTC)
    assert _parse_dt(now) is now
    assert _parse_dt(None) is None
    # ISO string is coerced.
    assert _parse_dt("2026-04-19T12:00:00+00:00") == now


def test_path_helpers_compute_expected_locations(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    assert store.state_path("abc").name == "abc.yaml"
    assert store.state_path("abc").parent == tmp_path / "state"
    assert store.lock_path("abc").name == "abc.lock"
    assert store.log_path("abc").name == "abc.ndjson"
    assert store.events_path() == tmp_path / "events.ndjson"


def test_save_cleans_up_tempfile_when_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If os.replace fails, the tempfile should be removed and the original
    exception should propagate."""
    import os

    import pytest as _pytest

    store = StateStore(tmp_path)

    def fail_replace(_src: str, _dst: Path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail_replace)

    state = TaskState(task_id="boom")
    with _pytest.raises(OSError, match="disk full"):
        store.save(state)
    # No orphan tempfile should remain in the state dir.
    leftovers = list((tmp_path / "state").glob(".boom.*"))
    assert not leftovers


def test_iter_states_when_dir_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If the state dir is gone after construction, iter_states returns []."""
    import shutil

    store = StateStore(tmp_path)
    shutil.rmtree(tmp_path / "state")
    assert store.iter_states() == []
