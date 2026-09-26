"""``dispatch()`` holds the supervisor's spawn gate while it starts a worker.

It enters the gate before it opens the attempt's log files, and leaves
once the worker's pid (and, when file-backed, its log path) is in the
task's state YAML. A stopping supervisor waits for the gate, so it never
exits leaving a worker that no one can find (see :mod:`runner.spawn_gate`).
A worker that fails to start leaves the gate too, so the stop does not
wait for it. These drive the real ``dispatch()`` against the bundled fake
``claude`` shim.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from claude_task_runner.clock import RealClock
from claude_task_runner.config.schema import HookSettings, TaskCapsSettings
from claude_task_runner.queue.schema import Task, TaskState
from claude_task_runner.queue.store import load_state, queue_runtime_dir, state_path_for
from claude_task_runner.runner.dispatcher import DispatchOutcome, dispatch
from claude_task_runner.runner.session import ResumeStrategy, SpawnPlan
from claude_task_runner.runner.spawn_gate import SpawnGate

SHIM_PATH = Path(__file__).parent.parent / "fixtures" / "claude_shim" / "claude"

# (call, stdout log exists, pid in the state YAML, log_path in the state YAML)
_Call = tuple[str, bool, int | None, str | None]


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    qd = tmp_path / "queue"
    qd.mkdir()
    queue_runtime_dir(qd)
    return qd


@pytest.fixture
def task() -> Task:
    return Task(id="001-gate", title="Gate", prompt="Do the thing", working_dir=None)


@pytest.fixture(autouse=True)
def _reset_shim_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("SHIM_"):
            monkeypatch.delenv(key)


def _stdout_log(queue_dir: Path, task_id: str) -> Path:
    """The first attempt's stdout log, spelled out: ``_attempt_log_paths``
    would create its directory."""
    return queue_dir / ".claude_task_runner" / "logs" / task_id / "attempt-1.stream.jsonl"


class _RecordingGate(SpawnGate):
    """Records, at each enter and leave, whether the attempt's stdout log
    exists and which pid and log path the task's state YAML holds."""

    def __init__(self, queue_dir: Path, task_id: str) -> None:
        super().__init__()
        self._queue_dir = queue_dir
        self._task_id = task_id
        self.calls: list[_Call] = []

    def _record(self, call: str) -> None:
        state = load_state(state_path_for(self._queue_dir, self._task_id))
        exists = _stdout_log(self._queue_dir, self._task_id).exists()
        self.calls.append((call, exists, state.pid, state.log_path))

    def enter(self, task_id: str) -> None:
        self._record("enter")
        super().enter(task_id)

    def leave(self, task_id: str) -> None:
        self._record("leave")
        super().leave(task_id)


def _dispatch(queue_dir: Path, task: Task, *, adopt: bool, gate: SpawnGate) -> DispatchOutcome:
    return dispatch(
        task=task,
        state=TaskState(task_id=task.id),
        plan=SpawnPlan(
            strategy=ResumeStrategy.FRESH, session_id=None, prompt=task.prompt, extra_args=[]
        ),
        queue_dir=queue_dir,
        clock=RealClock(),
        settings_caps=TaskCapsSettings(
            max_tokens_per_task=0,
            max_duration_s_per_task=0,
            heartbeat_silence_alert_s=600,
            heartbeat_silence_kill_s=0,
        ),
        settings_hooks=HookSettings(
            pre_dispatch_command="",
            pre_dispatch_timeout_s=10,
            post_dispatch_command="",
            post_dispatch_timeout_s=10,
        ),
        claude_executable=str(SHIM_PATH),
        adopt_workers=adopt,
        spawn_gate=gate,
    )


def test_a_file_backed_attempt_holds_the_gate_from_its_log_files_to_its_recorded_pid(
    queue_dir: Path, task: Task
) -> None:
    gate = _RecordingGate(queue_dir, task.id)

    outcome = _dispatch(queue_dir, task, adopt=True, gate=gate)

    assert outcome.new_state.status == "completed"
    assert gate.calls == [
        ("enter", False, None, None),
        ("leave", True, outcome.run_record.pid, str(_stdout_log(queue_dir, task.id))),
    ]
    assert gate.close(0.0) == []


def test_a_pipe_backed_attempt_holds_the_gate_until_its_pid_is_recorded(
    queue_dir: Path, task: Task
) -> None:
    """With adoption off there are no log files, and still a pid to record."""
    gate = _RecordingGate(queue_dir, task.id)

    outcome = _dispatch(queue_dir, task, adopt=False, gate=gate)

    assert outcome.new_state.status == "completed"
    assert gate.calls == [
        ("enter", False, None, None),
        ("leave", False, outcome.run_record.pid, None),
    ]
    assert gate.close(0.0) == []


def test_a_worker_that_fails_to_start_leaves_the_gate(
    queue_dir: Path, task: Task, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise a stop would wait out the whole bound for a worker that
    never started, and report it as abandoned."""

    def failing_popen(*_args: object, **_kwargs: object) -> subprocess.Popen[str]:
        raise OSError("exec failed")

    monkeypatch.setattr(subprocess, "Popen", failing_popen)
    gate = _RecordingGate(queue_dir, task.id)

    with pytest.raises(OSError, match=r"^exec failed$"):
        _dispatch(queue_dir, task, adopt=True, gate=gate)

    assert gate.calls == [
        ("enter", False, None, None),
        ("leave", True, None, None),
    ]
    assert gate.close(0.0) == []
