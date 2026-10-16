"""A parked worker past its duration cap is stopped, against a real process.

A worker parked ``possibly_hung`` (``worker_still_running``) has no monitor
thread to enforce its task's duration cap, so the per-tick parked-worker
pass does: it terminates the real worker's process group through the
reapers' terminate and records the run as killed by the cap.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from signal_gate import SignalGate

from claude_task_runner.clock import FakeClock
from claude_task_runner.config.loader import load_settings
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
from claude_task_runner.runner.dispatcher import park_live_worker
from claude_task_runner.supervisor.adoption import ParkedWorkerTracker

from ._file_worker import spawn_file_worker


@pytest.mark.slow
@pytest.mark.usefixtures("real_proc")
def test_parked_real_worker_past_its_cap_is_terminated_and_recorded(
    tmp_path: Path, signal_gate: SignalGate
) -> None:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    write_task_atomic(
        Task(id="t-capped", title="t", prompt="p", working_dir=None, max_duration_s_override=60),
        task_path_for(qd, "t-capped"),
    )
    log = qd / ".claude_task_runner" / "logs" / "t-capped" / "attempt-1.stream.jsonl"
    pid = spawn_file_worker(log, {"WORKER_SESSION_ID": "sess-cap", "WORKER_HOLD_S": "120"})
    signal_gate.allow(pid)
    now = datetime.now(UTC)
    write_state_atomic(
        park_live_worker(
            TaskState(
                task_id="t-capped",
                status="running",
                attempts=1,
                last_started_at=now - timedelta(hours=2),
                pid=pid,
                log_path=str(log),
            ),
            why="and was not adopted by this supervisor",
        ),
        state_path_for(qd, "t-capped"),
    )
    settings = load_settings(None)
    tracker = ParkedWorkerTracker()

    parked = tracker.tick(
        qd, settings=settings, clock=FakeClock(now), prior_in_flight=[], owned_ids=()
    )

    deadline = time.monotonic() + 10
    while dispatcher_mod._pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not dispatcher_mod._pid_alive(pid), "the capped worker's process group survived"
    assert parked.in_flight == []
    (job,) = parked.to_finalize
    tracker.finalize(job, qd, settings=settings, clock=FakeClock(now))
    state = load_state(state_path_for(qd, "t-capped"))
    assert (state.status, state.pid, [r.killed_by_cap for r in state.runs]) == (
        "failed",
        None,
        ["duration"],
    )
