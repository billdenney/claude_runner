"""Every pass that acts on a recorded worker asks the full worker probe.

``tests/conftest.py`` keeps :func:`runner.dispatcher.probe_worker` off the
host's ``/proc``, so most tests see only ``_pid_alive``. Here each call site
gets a fake ``/proc`` in which the recorded pid is alive but belongs to a
process that is not the worker, in two ways the probe tells apart:

* ``stdout``: the process's stdout is another file, though it started when
  the attempt did. Only the log comparison catches it.
* ``start_time``: its stdout cannot be read (another user's process) and it
  started two hours after the attempt. Only the start-time comparison
  catches it.

A call site that dropped either the log path or the start time, or probed
existence alone, would take that process for the worker.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from claude_task_runner.clock import FakeClock, RealClock
from claude_task_runner.config.loader import load_settings
from claude_task_runner.config.schema import Settings, TaskCapsSettings
from claude_task_runner.queue.schema import Task, TaskState
from claude_task_runner.queue.store import (
    load_state,
    queue_runtime_dir,
    state_path_for,
    todo_dir,
    write_state_atomic,
    write_task_atomic,
)
from claude_task_runner.runner import dispatcher as dispatcher_mod
from claude_task_runner.runner.dispatcher import WorkerProbe
from claude_task_runner.runner.heartbeat import HeartbeatVerdict
from claude_task_runner.runner.in_flight import DispatchSlot
from claude_task_runner.supervisor import reconcile_silent as rs
from claude_task_runner.supervisor.adoption import (
    adopt_running_workers,
    finalize_exited_workers,
)
from claude_task_runner.supervisor.reconcile import ORPHAN_STOP_REASON, reconcile_orphans
from claude_task_runner.supervisor.states import SupervisorSnapshot, SupervisorState

from ._worker_probe_stubs import fake_proc

_PID = 4_100_042
_NOW = datetime(2026, 10, 16, 14, 0, tzinfo=UTC)
_STARTED = _NOW - timedelta(hours=1)
_INIT = '{"type":"system","subtype":"init","session_id":"sess-w"}'
_RESULT = (
    '{"type":"result","subtype":"success","stop_reason":"end_turn","is_error":false,'
    '"total_cost_usd":0.01,"usage":{"input_tokens":1,"output_tokens":1}}'
)

VARIANTS = ["stdout", "start_time"]


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    return qd


def _settings(*, kill: float = 0.0) -> Settings:
    base = load_settings(None)
    return base.model_copy(
        update={
            "task_caps": base.task_caps.model_copy(update={"heartbeat_silence_kill_s": kill}),
        }
    )


def _seed(
    queue_dir: Path,
    task_id: str,
    *,
    lines: list[str],
    last_heartbeat_at: datetime | None = None,
    dispatcher_alive_at: datetime | None = None,
) -> TaskState:
    """A running attempt started an hour ago whose log was last written then."""
    write_task_atomic(
        Task(id=task_id, title="t", prompt="p", working_dir=None),
        queue_dir / "todo" / f"{task_id}.yaml",
    )
    log = queue_dir / ".claude_task_runner" / "logs" / task_id / "attempt-1.stream.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("".join(line + "\n" for line in lines))
    os.utime(log, (_STARTED.timestamp(), _STARTED.timestamp()))
    state = TaskState(
        task_id=task_id,
        status="running",
        attempts=1,
        last_started_at=_STARTED,
        last_heartbeat_at=last_heartbeat_at,
        dispatcher_alive_at=dispatcher_alive_at,
        pid=_PID,
        log_path=str(log),
    )
    write_state_atomic(state, state_path_for(queue_dir, task_id))
    return state


def _other_process(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, variant: str) -> None:
    """Give ``_PID`` to a live process that is not the recorded worker."""
    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda pid: pid == _PID)
    elsewhere = tmp_path / "elsewhere.out"
    elsewhere.write_text("")
    if variant == "stdout":
        root = fake_proc(
            tmp_path / "proc",
            _PID,
            started=_STARTED,
            boot=_STARTED - timedelta(days=1),
            stdout=elsewhere,
        )
    else:
        root = fake_proc(
            tmp_path / "proc",
            _PID,
            started=_STARTED + timedelta(hours=2),
            boot=_STARTED - timedelta(days=1),
        )
    monkeypatch.setattr(dispatcher_mod, "_PROC_ROOT", root)


def _snapshot() -> SupervisorSnapshot:
    return SupervisorSnapshot(state=SupervisorState.IDLE, since=_NOW)


@pytest.mark.parametrize("variant", VARIANTS)
def test_startup_reaper_does_not_signal_another_process(
    queue_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    """KILL verdict with the default probe: the pid is not the worker, so
    nothing is signalled and the task fails (its worker is gone)."""
    _seed(queue_dir, "t-kill", lines=[_INIT])
    _other_process(monkeypatch, tmp_path, variant)
    terminated: list[int] = []

    results = rs.reconcile_silent_orphans(
        queue_dir,
        settings=_settings(kill=900).task_caps,
        clock=FakeClock(_NOW),
        terminate_fn=lambda pid: terminated.append(pid) or True,
    )

    assert terminated == []
    assert [(r.verdict, r.sigtermed) for r in results] == [(HeartbeatVerdict.KILL, False)]
    assert load_state(state_path_for(queue_dir, "t-kill")).status == "failed"


@pytest.mark.parametrize("variant", VARIANTS)
def test_tick_reaper_does_not_scan_another_process(
    queue_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    """Monitor alive, agent silent past the stuck-loop threshold, default
    probe: another process's tree is never scanned or signalled."""
    _seed(
        queue_dir,
        "t-loop",
        lines=[_INIT],
        last_heartbeat_at=_NOW - timedelta(seconds=1000),
        dispatcher_alive_at=_NOW - timedelta(seconds=10),
    )
    _other_process(monkeypatch, tmp_path, variant)
    scanned: list[int] = []

    results = rs.reap_silent_orphans_tick(
        queue_dir,
        {"t-loop"},
        settings=_settings().task_caps,
        clock=FakeClock(_NOW),
        stuck_loop_detect_fn=lambda pid: scanned.append(pid) or (pid + 1, "while true; sleep 1"),
        terminate_fn=lambda _pid: True,
    )

    assert scanned == []
    assert results == []


@pytest.mark.parametrize("variant", VARIANTS)
def test_adoption_does_not_adopt_another_process(
    queue_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    """A fresh heartbeat would make the worker adoptable; the pid is not it."""
    _seed(queue_dir, "t-adopt", lines=[_INIT], last_heartbeat_at=_NOW - timedelta(seconds=10))
    _other_process(monkeypatch, tmp_path, variant)
    monkeypatch.setattr(
        dispatcher_mod,
        "adopt_worker",
        lambda **_kw: (_ for _ in ()).throw(AssertionError("adopted another process")),
    )
    slots: dict[str, DispatchSlot] = {}

    adopted = adopt_running_workers(
        queue_dir, settings=_settings(), clock=FakeClock(_NOW), in_flight_slots=slots
    )
    demoted = reconcile_orphans(queue_dir, _snapshot(), adopted_ids=set())[1]

    assert adopted == []
    assert slots == {}
    assert demoted == ["t-adopt"]
    state = load_state(state_path_for(queue_dir, "t-adopt"))
    assert (state.status, state.stop_reason) == ("failed", ORPHAN_STOP_REASON)


@pytest.mark.parametrize("variant", VARIANTS)
def test_exited_worker_finalize_sees_through_another_process(
    queue_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    """The worker finished during the restart; its pid now belongs to
    another process. Its run is recorded from its log."""
    _seed(queue_dir, "t-done", lines=[_INIT, _RESULT])
    _other_process(monkeypatch, tmp_path, variant)

    results = finalize_exited_workers(queue_dir, settings=_settings(), clock=FakeClock(_NOW))

    assert [(r.task_id, r.status) for r in results] == [("t-done", "completed")]


@pytest.mark.parametrize("variant", VARIANTS)
def test_adoption_monitor_stops_at_another_process(
    queue_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    """The monitor stops tailing at once and finalizes the crash, instead of
    watching another process until it exits."""
    state = _seed(queue_dir, "t-tail", lines=[_INIT])
    _other_process(monkeypatch, tmp_path, variant)
    sleeps: list[float] = []

    def no_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        assert len(sleeps) < 100, "still tailing a pid that is not the worker"

    settings = _settings()
    outcome = dispatcher_mod.adopt_worker(
        task=Task(id="t-tail", title="t", prompt="p", working_dir=None),
        state=state,
        queue_dir=queue_dir,
        clock=RealClock(),
        # No duration cap: the attempt's fixed start is hours before the
        # wall clock this test runs at.
        settings_caps=settings.task_caps.model_copy(update={"max_duration_s_per_task": 0}),
        settings_dispatch=settings.dispatch,
        settings_hooks=settings.hooks,
        sleep_fn=no_sleep,
    )

    assert sleeps == []
    assert outcome.run_record.stop_reason == "process_exit_nonzero"


def test_adoption_monitor_does_not_signal_a_pid_that_changed_hands(
    queue_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cap is breached on the first event, by which time the pid has gone
    to another process: the monitor's terminate checks again and signals
    nothing. The log's lines so far are read without a probe, so the
    terminate's is the first."""
    state = _seed(queue_dir, "t-cap", lines=[_INIT, _INIT])
    probes: list[WorkerProbe] = []

    def probe(_state: TaskState, _queue_dir: Path) -> WorkerProbe:
        probes.append(WorkerProbe.REUSED)
        return WorkerProbe.REUSED

    monkeypatch.setattr(dispatcher_mod, "probe_recorded_worker", probe)
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(
        dispatcher_mod, "_signal_group_by_pid", lambda pid, sig: signalled.append((pid, sig))
    )
    settings = _settings()

    outcome = dispatcher_mod.adopt_worker(
        task=Task(id="t-cap", title="t", prompt="p", working_dir=None),
        state=state,
        queue_dir=queue_dir,
        clock=RealClock(),
        settings_caps=TaskCapsSettings(
            max_tokens_per_task=0,
            max_duration_s_per_task=300.0,
            heartbeat_silence_alert_s=600,
            heartbeat_silence_kill_s=0,
            dispatcher_alive_write_interval_s=600.0,
        ),
        settings_dispatch=settings.dispatch,
        settings_hooks=settings.hooks,
        sleep_fn=lambda _s: None,
    )

    assert signalled == []
    assert probes == [WorkerProbe.REUSED]  # the terminate's, and no other
    assert outcome.run_record.killed_by_cap == "duration"


# ---------------------------------------------------------------------------
# The reapers' default terminate
# ---------------------------------------------------------------------------


def test_default_terminate_reports_a_survivor(monkeypatch: pytest.MonkeyPatch) -> None:
    """The escalation ran but the worker still runs (a D-state, or a group
    the supervisor may not signal): the kill is unconfirmed."""
    monkeypatch.setattr(dispatcher_mod, "_terminate_by_pid", lambda pid, **_kw: None)
    monkeypatch.setattr(
        dispatcher_mod, "probe_worker", lambda _pid, **_kw: dispatcher_mod.WorkerProbe.ALIVE
    )
    assert rs.terminate_worker(_PID) is False


def test_default_terminate_reports_a_failed_escalation(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def refused(pid: int, **_kw: object) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(dispatcher_mod, "_terminate_by_pid", refused)
    with caplog.at_level(logging.WARNING):
        assert rs.terminate_worker(_PID) is False
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == [
        f"silent-orphan reaper: _terminate_by_pid(pid={_PID}) raised "
        "[Errno 1] Operation not permitted; reporting unconfirmed kill"
    ]


def test_default_terminate_counts_a_zombie_as_gone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A killed worker that is not yet reaped still answers signal 0. It
    has exited, so the kill is confirmed rather than reported as a leak."""
    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda pid: pid == _PID)
    monkeypatch.setattr(
        dispatcher_mod,
        "_PROC_ROOT",
        fake_proc(tmp_path / "proc", _PID, started=_STARTED, boot=_STARTED, state="Z"),
    )
    sent: list[int] = []
    monkeypatch.setattr(dispatcher_mod, "_signal_group_by_pid", lambda pid, sig: sent.append(sig))
    start = time.monotonic()

    assert rs.terminate_worker(_PID) is True
    # Gone at the first check: no wait out the 5 s grace period, no SIGKILL.
    assert len(sent) == 1
    assert time.monotonic() - start < 2.0


def test_default_terminate_confirms_a_worker_that_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dispatcher_mod, "_terminate_by_pid", lambda pid, **_kw: None)
    monkeypatch.setattr(
        dispatcher_mod, "probe_worker", lambda _pid, **_kw: dispatcher_mod.WorkerProbe.DEAD
    )
    assert rs.terminate_worker(_PID) is True
