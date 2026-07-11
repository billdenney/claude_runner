"""Unit tests for the session-affinity TTL helpers (ADR-0024 extension).

After a Claude session has been idle past the configured TTL its
resume/cache value is spent, so the orchestrator clears it and lets the
task dispatch fresh on any account instead of staying stranded on a
throttled host. These tests cover the two decision/mutation helpers that
back that behaviour: ``_session_affinity_expired`` and
``_clear_session_for_fresh_dispatch``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from claude_task_runner.queue.schema import TaskState
from claude_task_runner.queue.store import (
    load_state,
    state_path_for,
    write_state_atomic,
)
from claude_task_runner.runner.orchestrator import (
    _clear_session_for_fresh_dispatch,
    _session_affinity_expired,
)

NOW = datetime(2026, 7, 11, 12, 0, 0, tzinfo=UTC)
TTL = 5400.0  # 1.5h


def _write(queue_dir: Path, task_id: str, **kw: object) -> Path:
    sp = state_path_for(queue_dir, task_id)
    sp.parent.mkdir(parents=True, exist_ok=True)
    write_state_atomic(TaskState(task_id=task_id, **kw), sp)
    return sp


def test_expired_when_session_idle_past_ttl(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "t1",
        session_id="s1",
        session_account="personal",
        last_finished_at=NOW - timedelta(hours=2),
    )
    assert _session_affinity_expired(tmp_path, "t1", now=NOW, ttl_seconds=TTL) is True


def test_not_expired_when_session_fresh(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "t2",
        session_id="s2",
        session_account="personal",
        last_finished_at=NOW - timedelta(minutes=30),
    )
    assert _session_affinity_expired(tmp_path, "t2", now=NOW, ttl_seconds=TTL) is False


def test_not_expired_without_session(tmp_path: Path) -> None:
    # No session_id -> nothing to expire, even if the timestamp is ancient.
    _write(tmp_path, "t3", last_finished_at=NOW - timedelta(hours=5))
    assert _session_affinity_expired(tmp_path, "t3", now=NOW, ttl_seconds=TTL) is False


def test_not_expired_when_no_timestamp(tmp_path: Path) -> None:
    _write(tmp_path, "t4", session_id="s4", session_account="personal")
    assert _session_affinity_expired(tmp_path, "t4", now=NOW, ttl_seconds=TTL) is False


def test_not_expired_when_no_state_file(tmp_path: Path) -> None:
    assert _session_affinity_expired(tmp_path, "missing", now=NOW, ttl_seconds=TTL) is False


def test_non_positive_ttl_never_expires(tmp_path: Path) -> None:
    # A misconfigured ttl of 0 must not drop affinity for every task.
    _write(
        tmp_path,
        "t5",
        session_id="s5",
        session_account="personal",
        last_finished_at=NOW - timedelta(days=30),
    )
    assert _session_affinity_expired(tmp_path, "t5", now=NOW, ttl_seconds=0.0) is False


def test_falls_back_to_heartbeat_when_no_finished(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "t6",
        session_id="s6",
        session_account="personal",
        last_heartbeat_at=NOW - timedelta(hours=2),
    )
    assert _session_affinity_expired(tmp_path, "t6", now=NOW, ttl_seconds=TTL) is True


def test_boundary_just_under_ttl_not_expired(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "t7",
        session_id="s7",
        session_account="personal",
        last_finished_at=NOW - timedelta(seconds=TTL - 1),
    )
    assert _session_affinity_expired(tmp_path, "t7", now=NOW, ttl_seconds=TTL) is False


def test_clear_session_resets_fields(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "t8",
        session_id="s8",
        session_account="personal",
        resume_attempts=3,
        last_finished_at=NOW - timedelta(hours=2),
    )
    _clear_session_for_fresh_dispatch(tmp_path, "t8")
    state = load_state(state_path_for(tmp_path, "t8"))
    assert state.session_id is None
    assert state.session_account is None
    assert state.resume_attempts == 0


def test_clear_session_missing_file_is_noop(tmp_path: Path) -> None:
    # Best-effort: a missing state file must not raise.
    _clear_session_for_fresh_dispatch(tmp_path, "missing")
