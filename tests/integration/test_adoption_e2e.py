"""End-to-end adoption against a real file-backed worker process (ADR-0025).

Spawns the ``file_worker.py`` shim as its own session-leader process
(modelling a ``claude --print`` worker the dispatcher redirected to a
log file), records its pid + log path on a running ``TaskState``, then
re-attaches with ``adopt_worker`` — exactly what the supervisor's
startup adoption does. The worker runs to completion (writing a terminal
result event), the adopter tails the live file to the end, infers the
outcome, and finalizes the task as completed with a RunRecord persisted.

This exercises the real tailer, the real ``_pid_alive`` probe, the real
``_DispatcherAliveMonitor`` thread, and the real finalize — no stubbing.
"""

from __future__ import annotations

import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_task_runner.clock import RealClock
from claude_task_runner.config.schema import TaskCapsSettings
from claude_task_runner.queue.schema import Task, TaskState
from claude_task_runner.queue.store import (
    load_state,
    queue_runtime_dir,
    state_path_for,
    write_state_atomic,
)
from claude_task_runner.runner.dispatcher import adopt_worker

WORKER = Path(__file__).parent.parent / "fixtures" / "claude_shim" / "file_worker.py"


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    qd = tmp_path / "queue"
    qd.mkdir()
    queue_runtime_dir(qd)
    return qd


def _caps() -> TaskCapsSettings:
    return TaskCapsSettings(
        max_tokens_per_task=0,
        max_duration_s_per_task=0,
        heartbeat_silence_alert_s=600,
        heartbeat_silence_kill_s=0,
        dispatcher_alive_write_interval_s=600.0,
    )


def _spawn_worker(log_path: Path, env_extra: dict[str, str]) -> int:
    """Launch the file-backed worker; return the (init-owned) worker pid.

    The launcher forks the real worker, prints its pid, and exits, so the
    worker is re-parented to init (no zombie for this test process) and
    its pid genuinely disappears on exit — which is what the adopter's
    ``os.kill(pid, 0)`` liveness probe needs.
    """
    import os

    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **env_extra}
    out = subprocess.run(
        [sys.executable, str(WORKER), str(log_path)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    return int(out.stdout.strip())


def _seed_running(queue_dir: Path, task: Task, log: Path, pid: int) -> TaskState:
    state = TaskState(
        task_id=task.id,
        status="running",
        last_started_at=datetime.now(UTC),
        pid=pid,
        log_path=str(log),
    )
    write_state_atomic(state, state_path_for(queue_dir, task.id))
    return state


def test_adopt_real_worker_to_completion(queue_dir: Path) -> None:
    """A live worker process writing stream-json to its log is adopted and
    finalized as completed once it exits, with the RunRecord persisted."""
    task = Task(id="900-adopt-e2e", title="t", prompt="p", working_dir=None)
    log = queue_dir / ".claude_task_runner" / "logs" / task.id / "attempt-1.stream.jsonl"

    # Worker sleeps briefly before writing, so adoption attaches while it
    # is still live and quiet (the tailer polls), then drains its output
    # once it writes and exits.
    pid = _spawn_worker(
        log,
        {
            "WORKER_SESSION_ID": "adopt-live",
            "WORKER_STOP_REASON": "end_turn",
            "WORKER_PRESLEEP_S": "0.4",
        },
    )
    state = _seed_running(queue_dir, task, log, pid)

    outcome = adopt_worker(
        task=task,
        state=state,
        queue_dir=queue_dir,
        clock=RealClock(),
        settings_caps=_caps(),
    )

    assert outcome.new_state.status == "completed"
    assert outcome.run_record.stop_reason == "end_turn"
    assert outcome.run_record.error is None
    assert outcome.summary.session_id == "adopt-live"
    # Usage inferred from the terminal result event in the log.
    assert outcome.run_record.usage.input_tokens == 30
    assert outcome.run_record.usage.output_tokens == 20

    # Persisted terminal state: completed, with the RunRecord appended and
    # pid/log_path cleared.
    reloaded = load_state(state_path_for(queue_dir, task.id))
    assert reloaded.status == "completed"
    assert len(reloaded.runs) == 1
    assert reloaded.runs[0].stop_reason == "end_turn"
    assert reloaded.pid is None
    assert reloaded.log_path is None


def test_adopt_real_worker_crash_no_result_failed(queue_dir: Path) -> None:
    """A worker that exits WITHOUT a terminal result event (crash mid-run)
    is finalized as failed — the exit code is inferred non-zero because
    there's no result to classify."""
    task = Task(id="901-adopt-crash-e2e", title="t", prompt="p", working_dir=None)
    log = queue_dir / ".claude_task_runner" / "logs" / task.id / "attempt-1.stream.jsonl"

    pid = _spawn_worker(
        log,
        {"WORKER_SESSION_ID": "adopt-crash", "WORKER_NO_RESULT": "1", "WORKER_PRESLEEP_S": "0.2"},
    )
    state = _seed_running(queue_dir, task, log, pid)

    outcome = adopt_worker(
        task=task,
        state=state,
        queue_dir=queue_dir,
        clock=RealClock(),
        settings_caps=_caps(),
    )

    assert outcome.new_state.status == "failed"
    assert outcome.run_record.stop_reason == "process_exit_nonzero"
    # The session id was still captured from the init line before the crash.
    assert outcome.summary.session_id == "adopt-crash"

    reloaded = load_state(state_path_for(queue_dir, task.id))
    assert reloaded.status == "failed"
    assert reloaded.pid is None


def test_adopt_waits_for_live_worker_then_finalizes(queue_dir: Path) -> None:
    """Sanity on liveness: while the worker is mid-presleep the adopter is
    actively tailing (not finalizing); it only finalizes after the worker
    exits. We assert the call blocks at least roughly the presleep before
    returning, proving it didn't short-circuit on a live pid."""
    task = Task(id="902-adopt-wait", title="t", prompt="p", working_dir=None)
    log = queue_dir / ".claude_task_runner" / "logs" / task.id / "attempt-1.stream.jsonl"

    pid = _spawn_worker(log, {"WORKER_SESSION_ID": "adopt-wait", "WORKER_PRESLEEP_S": "0.6"})
    state = _seed_running(queue_dir, task, log, pid)

    start = time.monotonic()
    outcome = adopt_worker(
        task=task,
        state=state,
        queue_dir=queue_dir,
        clock=RealClock(),
        settings_caps=_caps(),
    )
    elapsed = time.monotonic() - start

    # It tailed the live worker for at least most of the presleep window.
    assert elapsed >= 0.4
    assert outcome.new_state.status == "completed"
