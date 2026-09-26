"""A stopping supervisor waits for a worker it is starting (ADR-0025).

With ``[supervisor].adopt_workers`` on, dispatch threads are daemon
threads and ``start_daemon`` exits on SIGTERM without joining them. Its
sleep between ticks ends within half a second of the signal, so an exit
can land between a thread's ``Popen`` and the state write that records the
worker's pid. The next supervisor could then neither adopt that worker nor
see it, and would dispatch its task a second time. These run the real
loop, dispatch one task to the fake ``claude`` shim with the shim's
``Popen`` held until the test lets it go, and send SIGTERM meanwhile.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from claude_task_runner.config.loader import load_settings
from claude_task_runner.config.schema import Settings
from claude_task_runner.queue.schema import Task
from claude_task_runner.queue.store import (
    load_state,
    queue_runtime_dir,
    state_path_for,
    task_path_for,
    todo_dir,
    write_task_atomic,
)
from claude_task_runner.runner import dispatcher as dispatcher_mod
from claude_task_runner.runner.spawn_gate import SpawnGate
from claude_task_runner.supervisor import daemon as daemon_mod
from claude_task_runner.supervisor.daemon import start_daemon
from claude_task_runner.usage.models import UsageReading, WindowReading
from claude_task_runner.usage.source import FakeUsageSource

pytestmark = pytest.mark.usefixtures("harmless_signal_handlers", "private_global_lock")

SHIM_PATH = Path(__file__).parent.parent / "fixtures" / "claude_shim" / "claude"
TASK_ID = "t1"
# How long the test holds the worker's start after the stop. A supervisor
# that did not wait would return long before this.
_HOLD_AFTER_STOP_S = 2.0
# How long a test watches for a worker start that must not happen.
_WATCH_S = 1.0


@pytest.fixture(autouse=True)
def _reset_shim_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("SHIM_"):
            monkeypatch.delenv(key)


def _queue(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    write_task_atomic(
        Task(id=TASK_ID, title="t", prompt="p", working_dir=None), task_path_for(qd, TASK_ID)
    )
    return qd


def _settings() -> Settings:
    """Adoption on (the default), the shim as ``claude``, and a poll
    interval long enough that the loop is asleep when the test stops it."""
    base = load_settings(None)
    return base.model_copy(
        update={
            "usage": base.usage.model_copy(update={"poll_interval_s": 30.0}),
            "claude": base.claude.model_copy(update={"executable": str(SHIM_PATH)}),
        }
    )


def _reading() -> UsageReading:
    return UsageReading(
        captured_at=datetime(2026, 9, 26, 12, 0, tzinfo=UTC),
        five_hour=WindowReading(
            utilization_pct=20,
            resets_at_raw="x",
            resets_at=datetime(2026, 9, 26, 17, 0, tzinfo=UTC),
        ),
        seven_day=WindowReading(
            utilization_pct=20,
            resets_at_raw="x",
            resets_at=datetime(2026, 9, 30, 12, 0, tzinfo=UTC),
        ),
    )


class _HeldPopen:
    """Stands in for ``subprocess.Popen``. Holds the shim's start until
    :attr:`release` is set, then starts it for real; every other call goes
    straight through."""

    def __init__(self) -> None:
        self._real = subprocess.Popen
        self.held = threading.Event()
        self.release = threading.Event()
        self.started: list[int] = []

    def __call__(self, argv: list[str], *args: Any, **kwargs: Any) -> Any:
        if argv and argv[0] == str(SHIM_PATH):
            self.held.set()
            if not self.release.wait(timeout=60):
                raise RuntimeError("the test never released the held Popen")
            process = self._real(argv, *args, **kwargs)
            self.started.append(process.pid)
            return process
        return self._real(argv, *args, **kwargs)


def _hold_popen(monkeypatch: pytest.MonkeyPatch) -> _HeldPopen:
    popen = _HeldPopen()
    monkeypatch.setattr(subprocess, "Popen", popen)
    return popen


def _spy_on_leave(
    monkeypatch: pytest.MonkeyPatch, queue_dir: Path
) -> list[tuple[float, int | None]]:
    """Record the time of each :meth:`SpawnGate.leave`, and the pid the
    task's state YAML holds at that moment."""
    leaves: list[tuple[float, int | None]] = []
    real_leave = SpawnGate.leave

    def recording_leave(self: SpawnGate, task_id: str) -> None:
        leaves.append((time.monotonic(), load_state(state_path_for(queue_dir, task_id)).pid))
        real_leave(self, task_id)

    monkeypatch.setattr(SpawnGate, "leave", recording_leave)
    return leaves


def _run(queue_dir: Path, fire: Callable[[], None]) -> float:
    """Run ``start_daemon`` in this (the main) thread with ``fire`` in a
    thread beside it; return the monotonic time it returned at."""
    thread = threading.Thread(target=fire, daemon=True)
    thread.start()
    try:
        start_daemon(
            queue_dir=queue_dir,
            settings=_settings(),
            source=FakeUsageSource([_reading()]),
            pending_count_fn=lambda: 1,
            in_flight_count_fn=lambda: 0,
            install_signal_handlers=True,
            # Only a backstop, in case a signal is lost.
            max_ticks=2,
        )
        return time.monotonic()
    finally:
        thread.join(timeout=60)


def _dispatch_thread() -> threading.Thread:
    (thread,) = [t for t in threading.enumerate() if t.name == f"dispatch-{TASK_ID}"]
    return thread


def _stop(sent: dict[str, float]) -> None:
    sent["stop"] = time.monotonic()
    os.kill(os.getpid(), signal.SIGTERM)


def test_a_stop_waits_until_the_worker_being_started_has_its_pid_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue_dir = _queue(tmp_path)
    popen = _hold_popen(monkeypatch)
    leaves = _spy_on_leave(monkeypatch, queue_dir)
    sent: dict[str, float] = {}

    def fire() -> None:
        if popen.held.wait(timeout=30):
            _stop(sent)
            time.sleep(_HOLD_AFTER_STOP_S)
            sent["release"] = time.monotonic()
            popen.release.set()

    returned_at = _run(queue_dir, fire)
    # The worker (the shim) was released and runs to the end; let its
    # thread record the run before the test's directory goes.
    _dispatch_thread().join(timeout=30)

    assert set(sent) == {"stop", "release"}, "the worker's start was never held"
    assert returned_at > sent["release"]
    assert len(popen.started) == 1
    assert [pid for _, pid in leaves] == popen.started
    assert leaves[0][0] < returned_at


def test_a_stop_gives_up_on_a_worker_start_after_the_bound_and_says_which(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(daemon_mod, "WORKER_START_WAIT_S", 0.5)
    queue_dir = _queue(tmp_path)
    popen = _hold_popen(monkeypatch)
    sent: dict[str, float] = {}

    def fire() -> None:
        if popen.held.wait(timeout=30):
            _stop(sent)

    with caplog.at_level(logging.ERROR, logger="claude_task_runner.supervisor.daemon"):
        returned_at = _run(queue_dir, fire)
    # Now let the held start go on, and its thread finish, before the
    # test's directory goes.
    popen.release.set()
    _dispatch_thread().join(timeout=30)

    assert "stop" in sent, "the worker's start was never held"
    # One slice of the sleep (0.5 s) and the bound (0.5 s), with slack,
    # and far short of the test's 60 s hold.
    assert returned_at - sent["stop"] < 5.0
    daemon_errors = [
        r.getMessage()
        for r in caplog.records
        if r.name == "claude_task_runner.supervisor.daemon" and r.levelno >= logging.ERROR
    ]
    assert daemon_errors == [
        "supervisor exiting with 1 worker(s) still starting after 0.5 s and no pid "
        "on record: ['t1']. The next supervisor cannot adopt them and will dispatch "
        "these tasks again; check each for a second claude process"
    ]


def test_a_dispatch_thread_that_reaches_the_gate_after_the_stop_starts_no_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Held before the gate while the supervisor stops: the stop does not
    wait for it, and once let go it starts nothing. Its task stays
    ``running`` with no pid, which the next supervisor demotes and
    dispatches again, once."""
    queue_dir = _queue(tmp_path)
    popen = _hold_popen(monkeypatch)
    popen.release.set()
    before_gate = threading.Event()
    let_go = threading.Event()
    real_pre_sha = dispatcher_mod._snapshot_pre_dispatch_sha

    def held_pre_sha(working_dir: Path | None) -> str | None:
        # dispatch() takes this snapshot just before it enters the gate.
        before_gate.set()
        let_go.wait(timeout=60)
        return real_pre_sha(working_dir)

    monkeypatch.setattr(dispatcher_mod, "_snapshot_pre_dispatch_sha", held_pre_sha)
    sent: dict[str, float] = {}

    def fire() -> None:
        if before_gate.wait(timeout=30):
            _stop(sent)

    returned_at = _run(queue_dir, fire)
    let_go.set()
    time.sleep(_WATCH_S)

    assert "stop" in sent, "the dispatch thread never got near the gate"
    assert returned_at - sent["stop"] < 5.0
    assert popen.started == []
    state = load_state(state_path_for(queue_dir, TASK_ID))
    assert (state.status, state.attempts, state.pid, state.log_path) == ("running", 1, None, None)
    # Parked at the closed gate until the process exits.
    assert _dispatch_thread().is_alive()
