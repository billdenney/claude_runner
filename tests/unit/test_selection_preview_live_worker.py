"""The read-only selector reports a live worker's task without parking it.

A supervisor tick parks a dispatchable task whose recorded worker still
runs (``live_worker``). ``preview_selection``, which ``queue holds`` runs,
must count the task the same way and write nothing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn

import pytest

from claude_task_runner.config.loader import load_settings
from claude_task_runner.queue import store as store_mod
from claude_task_runner.queue.schema import Task, TaskState
from claude_task_runner.queue.store import (
    queue_runtime_dir,
    state_path_for,
    task_path_for,
    todo_dir,
    write_state_atomic,
    write_task_atomic,
)
from claude_task_runner.runner import dispatcher as dispatcher_mod
from claude_task_runner.runner import orchestrator as orch_mod

from ._concurrency_fixtures import invoke_cli
from ._worker_probe_stubs import fake_proc

_PID = 4_600_003
_NOW = datetime(2026, 10, 17, 10, 0, tzinfo=UTC)


@pytest.fixture
def queue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A queue whose ``failed`` task records a worker, ``_PID``, that still
    runs with the task's log as its stdout."""
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    write_task_atomic(Task(id="t-live", title="t", prompt="p"), task_path_for(qd, "t-live"))
    log = qd / ".claude_task_runner" / "logs" / "t-live" / "attempt-1.stream.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text("")
    write_state_atomic(
        TaskState(task_id="t-live", status="failed", attempts=1, pid=_PID, log_path=str(log)),
        state_path_for(qd, "t-live"),
    )
    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda pid: pid == _PID)
    monkeypatch.setattr(
        dispatcher_mod,
        "_PROC_ROOT",
        fake_proc(
            tmp_path / "proc",
            _PID,
            started=_NOW - timedelta(minutes=5),
            boot=_NOW - timedelta(days=1),
            stdout=log,
        ),
    )
    return qd


@pytest.fixture
def state_writes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every writer of a state YAML raises, and records the attempt: the
    selector's park logs a failed write rather than raising it, so the
    record is what shows a write was tried."""
    attempts: list[str] = []

    def refuse(state: TaskState, *_args: Any, **_kwargs: Any) -> NoReturn:
        attempts.append(state.task_id)
        raise AssertionError("the read-only selector wrote a state file")

    for module in (orch_mod, dispatcher_mod, store_mod):
        monkeypatch.setattr(module, "write_state_atomic", refuse)
    return attempts


def _state_bytes(queue: Path) -> bytes:
    return state_path_for(queue, "t-live").read_bytes()


def test_preview_counts_a_live_workers_task_and_parks_nothing(
    queue: Path, state_writes: list[str]
) -> None:
    before = _state_bytes(queue)

    preview = orch_mod.preview_selection(queue, load_settings(None), snapshot=None, now=_NOW)

    assert preview.eligible == []
    assert preview.skips["live_worker"] == 1
    assert state_writes == []
    assert _state_bytes(queue) == before


def test_queue_holds_reports_a_live_workers_task_and_parks_nothing(
    queue: Path, state_writes: list[str]
) -> None:
    before = _state_bytes(queue)

    result = invoke_cli(["queue", "holds", "--queue", str(queue), "--json"])

    assert result.exit_code == 0, result.output
    holds = json.loads(result.stdout)
    assert holds["held"] == {"live_worker": 1}
    assert holds["eligible"]["count"] == 0
    assert state_writes == []
    assert _state_bytes(queue) == before
