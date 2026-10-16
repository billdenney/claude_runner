"""A state whose worker is gone keeps no pointer to it.

Every pass that sets a task ``failed`` or SILENT-parks it because its
worker is gone clears both ``pid`` and ``log_path``: the reapers' KILL
verdict, stuck-sleep-loop kill and SILENT park, and ``reconcile_orphans``.
A stale log path would make the worker probe compare a later worker's
stdout with that log.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from claude_task_runner.clock import FakeClock
from claude_task_runner.config.schema import TaskCapsSettings
from claude_task_runner.queue.schema import Task, TaskState
from claude_task_runner.queue.store import (
    load_state,
    queue_runtime_dir,
    state_path_for,
    task_path_for,
    todo_dir,
    write_state_atomic,
    write_task_atomic,
)
from claude_task_runner.runner import dispatcher as dispatcher_mod
from claude_task_runner.runner.dispatcher import WorkerProbe
from claude_task_runner.supervisor.reconcile import reconcile_orphans
from claude_task_runner.supervisor.reconcile_silent import (
    reap_silent_orphans_tick,
    reconcile_silent_orphans,
)
from claude_task_runner.supervisor.states import SupervisorSnapshot, SupervisorState

from ._worker_probe_stubs import probes

_NOW = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)
_PID = 4_500_001


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    write_task_atomic(Task(id="t", title="t", prompt="p"), task_path_for(qd, "t"))
    return qd


def _seed(queue_dir: Path, **fields: object) -> None:
    write_state_atomic(
        TaskState(
            task_id="t",
            status="running",
            attempts=1,
            last_started_at=_NOW - timedelta(seconds=3600),
            pid=_PID,
            log_path=str(queue_dir / "logs-of-t" / "attempt-1.stream.jsonl"),
            **fields,  # type: ignore[arg-type]
        ),
        state_path_for(queue_dir, "t"),
    )


def _caps(kill: float) -> TaskCapsSettings:
    return TaskCapsSettings(
        max_tokens_per_task=0,
        max_duration_s_per_task=0,
        heartbeat_silence_alert_s=300,
        heartbeat_silence_kill_s=kill,
        bash_poll_antipattern_kill=True,
    )


def _worker(queue_dir: Path) -> tuple[str, int | None, str | None]:
    state = load_state(state_path_for(queue_dir, "t"))
    return state.status, state.pid, state.log_path


@pytest.mark.parametrize("kill", [0.0, 900.0], ids=["silent", "kill"])
def test_startup_reaper_clears_a_gone_workers_pid_and_log(queue_dir: Path, kill: float) -> None:
    _seed(queue_dir)
    reconcile_silent_orphans(
        queue_dir,
        settings=_caps(kill),
        clock=FakeClock(_NOW),
        terminate_fn=lambda _pid: True,
        worker_probe_fn=probes(WorkerProbe.DEAD),
    )
    assert _worker(queue_dir) == ("possibly_hung" if kill == 0 else "failed", None, None)


def test_stuck_loop_kill_clears_the_workers_pid_and_log(queue_dir: Path) -> None:
    _seed(
        queue_dir,
        last_heartbeat_at=_NOW - timedelta(seconds=1000),
        dispatcher_alive_at=_NOW - timedelta(seconds=10),
    )
    reap_silent_orphans_tick(
        queue_dir,
        {"t"},
        settings=_caps(0),
        clock=FakeClock(_NOW),
        stuck_loop_detect_fn=lambda pid: (pid + 1, "while true; do sleep 1; done"),
        terminate_fn=lambda _pid: True,
        worker_probe_fn=probes(WorkerProbe.ALIVE, WorkerProbe.DEAD),
    )
    assert _worker(queue_dir) == ("failed", None, None)


def test_reconcile_orphans_clears_a_gone_workers_pid_and_log(
    queue_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda _pid: False)
    _seed(queue_dir)
    reconcile_orphans(queue_dir, SupervisorSnapshot(state=SupervisorState.IDLE, since=_NOW))
    assert _worker(queue_dir) == ("failed", None, None)
