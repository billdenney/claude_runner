"""Tests for the corrupt-state-file quarantine (ADR-0028).

A ``TaskState`` YAML left unparseable by a crash / power-loss mid-write
is silently skipped by every recovery sweep, wedging the task forever (a
"corrupt-state zombie"). :func:`quarantine_corrupt_state_files` moves the
file into ``state/.corrupt/`` so the task reverts to pending and
re-dispatches; a salvageable ``completed`` status is preserved.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_task_runner.queue.schema import TaskState
from claude_task_runner.queue.store import (
    QueueSchemaError,
    load_state,
    queue_runtime_dir,
    state_dir,
    state_path_for,
    todo_dir,
    write_state_atomic,
)
from claude_task_runner.supervisor.reconcile_corrupt import (
    CORRUPT_DIRNAME,
    CORRUPT_STOP_REASON,
    quarantine_corrupt_state_files,
)

_NOW = datetime(2026, 6, 29, 1, 30, tzinfo=UTC)

# A faithful copy of the corruption observed in the wild: valid top-level
# scalar fields (so the real status is recoverable), then a stale
# multi-line-string fragment wedged before ``runs:`` — exactly what a
# truncation-less rewrite of a shorter ``error:`` leaves behind. The lone
# ``:`` in the fragment is what makes PyYAML raise "mapping values are not
# allowed here".
_CORRUPT_TEMPLATE = """\
schema_version: 2
task_id: {task_id}
status: {status}
session_id: null
session_account: null
attempts: 0
resume_attempts: 0
last_started_at: null
last_finished_at: null
last_heartbeat_at: null
stop_reason: null
error: null
  \\ \\u2014 input awaits re-acquisition: /home/u/papers/{task_id}.pdf"
runs: []
"""


def _queue(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    state_dir(qd).mkdir(parents=True, exist_ok=True)
    return qd


def _seed_valid(qd: Path, task_id: str, *, status: str = "pending") -> None:
    write_state_atomic(TaskState(task_id=task_id, status=status), state_path_for(qd, task_id))


def _seed_corrupt(qd: Path, task_id: str, *, status: str = "pending") -> Path:
    p = state_path_for(qd, task_id)
    p.write_text(_CORRUPT_TEMPLATE.format(task_id=task_id, status=status), encoding="utf-8")
    return p


def test_corrupt_template_is_actually_unparseable(tmp_path: Path) -> None:
    qd = _queue(tmp_path)
    p = _seed_corrupt(qd, "t-bad")
    with pytest.raises(QueueSchemaError):
        load_state(p)


def test_clean_state_dir_is_noop(tmp_path: Path) -> None:
    qd = _queue(tmp_path)
    _seed_valid(qd, "t-ok-1")
    _seed_valid(qd, "t-ok-2", status="completed")
    results = quarantine_corrupt_state_files(qd, now=_NOW)
    assert results == []
    # both valid files still present and parseable
    assert load_state(state_path_for(qd, "t-ok-1")).status == "pending"
    assert load_state(state_path_for(qd, "t-ok-2")).status == "completed"
    assert not (state_dir(qd) / CORRUPT_DIRNAME).exists()


def test_corrupt_pending_reverts_to_pending(tmp_path: Path) -> None:
    qd = _queue(tmp_path)
    _seed_corrupt(qd, "t-bad", status="pending")
    results = quarantine_corrupt_state_files(qd, now=_NOW)

    assert len(results) == 1
    assert results[0].task_id == "t-bad"
    assert results[0].preserved_status is None
    # original gone from the state dir => no-state == pending == dispatchable
    assert not state_path_for(qd, "t-bad").exists()
    # quarantined copy preserved for forensics
    corrupt_dir = state_dir(qd) / CORRUPT_DIRNAME
    parked = list(corrupt_dir.glob("t-bad.*.yaml"))
    assert len(parked) == 1
    assert "input awaits re-acquisition" in parked[0].read_text()


def test_corrupt_completed_is_salvaged(tmp_path: Path) -> None:
    qd = _queue(tmp_path)
    _seed_corrupt(qd, "t-done", status="completed")
    results = quarantine_corrupt_state_files(qd, now=_NOW)

    assert len(results) == 1
    assert results[0].preserved_status == "completed"
    # a fresh VALID state replaced the corrupt one, preserving completion
    sp = state_path_for(qd, "t-done")
    assert sp.exists()
    rebuilt = load_state(sp)
    assert rebuilt.status == "completed"
    assert rebuilt.stop_reason == CORRUPT_STOP_REASON
    # and the corrupt original is quarantined
    assert list((state_dir(qd) / CORRUPT_DIRNAME).glob("t-done.*.yaml"))


def test_corrupt_failed_reverts_to_pending_not_preserved(tmp_path: Path) -> None:
    # Only ``completed`` is salvaged; a corrupt ``failed`` reverts to
    # pending (re-dispatch is harmless / desirable after corruption).
    qd = _queue(tmp_path)
    _seed_corrupt(qd, "t-failed", status="failed")
    results = quarantine_corrupt_state_files(qd, now=_NOW)
    assert results[0].preserved_status is None
    assert not state_path_for(qd, "t-failed").exists()


def test_mixed_dir_only_corrupt_files_touched(tmp_path: Path) -> None:
    qd = _queue(tmp_path)
    _seed_valid(qd, "t-ok", status="running")
    _seed_corrupt(qd, "t-bad-1")
    _seed_corrupt(qd, "t-bad-2")
    results = quarantine_corrupt_state_files(qd, now=_NOW)

    assert {r.task_id for r in results} == {"t-bad-1", "t-bad-2"}
    # the valid file is untouched
    assert load_state(state_path_for(qd, "t-ok")).status == "running"


def test_idempotent_second_pass_is_noop(tmp_path: Path) -> None:
    # The quarantine dir is a dot-subdir; list_state_files globs *.yaml
    # non-recursively, so a re-run must NOT re-scan parked files.
    qd = _queue(tmp_path)
    _seed_corrupt(qd, "t-bad")
    first = quarantine_corrupt_state_files(qd, now=_NOW)
    assert len(first) == 1
    second = quarantine_corrupt_state_files(qd, now=_NOW)
    assert second == []
    # still exactly one parked copy (not re-quarantined)
    assert len(list((state_dir(qd) / CORRUPT_DIRNAME).glob("t-bad.*.yaml"))) == 1


def test_repeat_corruption_same_task_keeps_both_copies(tmp_path: Path) -> None:
    qd = _queue(tmp_path)
    _seed_corrupt(qd, "t-bad")
    quarantine_corrupt_state_files(qd, now=_NOW)
    # task re-dispatches, corrupts again at the same wall-clock stamp
    _seed_corrupt(qd, "t-bad")
    quarantine_corrupt_state_files(qd, now=_NOW)
    parked = list((state_dir(qd) / CORRUPT_DIRNAME).glob("t-bad.*.yaml"))
    assert len(parked) == 2  # unique-suffix collision avoidance
