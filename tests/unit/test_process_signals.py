"""The runner never signals pid or group 1 or less, or its own pid or group.

On 2026-09-26 the test suite sent SIGTERM and SIGKILL to process group 1,
which is kill(-1), and killed every process of the user running it. Every
test here replaces ``os.kill``, ``os.killpg`` and ``os.getpgid`` with
recorders first, so a guard that failed would still send nothing.
"""

from __future__ import annotations

import ast
import os
import signal
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

import claude_task_runner
from claude_task_runner import process_signals
from claude_task_runner.clock import FakeClock
from claude_task_runner.config.schema import TaskCapsSettings
from claude_task_runner.process_signals import UnsafeSignalTarget
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
from claude_task_runner.runner.heartbeat import HeartbeatVerdict
from claude_task_runner.supervisor import reconcile_silent as rs

INIT = "pid 1 is init, and process group 1 is every process the user owns"
OWN_GROUP_PID = "pid 0 means this process's own process group"
NEGATIVE_PID = "a negative pid is a process group, and -1 is every process the user owns"
LOGGER = "claude_task_runner.process_signals"


class Recorder:
    """Stands in for ``os.kill``, ``os.killpg`` and ``os.getpgid``."""

    def __init__(self, pgid: int = 4242) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.pgid = pgid

    def kill(self, pid: int, sig: int) -> None:
        self.calls.append(("kill", pid, sig))

    def killpg(self, pgid: int, sig: int) -> None:
        self.calls.append(("killpg", pgid, sig))

    def getpgid(self, pid: int) -> int:
        self.calls.append(("getpgid", pid))
        return self.pgid


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    recorder = Recorder()
    monkeypatch.setattr(os, "kill", recorder.kill)
    monkeypatch.setattr(os, "killpg", recorder.killpg)
    monkeypatch.setattr(os, "getpgid", recorder.getpgid)
    return recorder


def _errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelname == "ERROR"]


# ---------------------------------------------------------------------------
# The guard itself: one known answer per refusal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pid", "hazard"),
    [(1, INIT), (0, OWN_GROUP_PID), (-1, NEGATIVE_PID), (-4242, NEGATIVE_PID)],
)
def test_kill_refuses_a_pid_of_one_or_less(
    sent: Recorder, caplog: pytest.LogCaptureFixture, pid: int, hazard: str
) -> None:
    message = f"refusing to send SIGTERM to pid {pid}: {hazard}"
    with pytest.raises(UnsafeSignalTarget) as exc_info:
        process_signals.kill(pid, signal.SIGTERM)
    assert str(exc_info.value) == message
    assert _errors(caplog) == [message]
    assert sent.calls == []


def test_kill_refuses_its_own_pid(sent: Recorder, caplog: pytest.LogCaptureFixture) -> None:
    message = f"refusing to send SIGKILL to pid {os.getpid()}: it is this process"
    with pytest.raises(UnsafeSignalTarget) as exc_info:
        process_signals.kill(os.getpid(), signal.SIGKILL)
    assert str(exc_info.value) == message
    assert _errors(caplog) == [message]
    assert sent.calls == []


def test_a_probe_is_refused_the_same_way(sent: Recorder, caplog: pytest.LogCaptureFixture) -> None:
    with pytest.raises(UnsafeSignalTarget):
        process_signals.kill(1, 0)
    assert _errors(caplog) == [f"refusing to probe pid 1: {INIT}"]
    assert sent.calls == []


@pytest.mark.parametrize(
    ("pgid", "hazard"),
    [
        (1, "killpg(1) is kill(-1), which signals every process the user owns"),
        (0, "process group 0 means this process's own group"),
        (-5, "a process group id is never negative"),
    ],
)
def test_killpg_refuses_a_group_of_one_or_less(
    sent: Recorder, caplog: pytest.LogCaptureFixture, pgid: int, hazard: str
) -> None:
    message = f"refusing to send SIGTERM to process group {pgid}: {hazard}"
    with pytest.raises(UnsafeSignalTarget) as exc_info:
        process_signals.killpg(pgid, signal.SIGTERM)
    assert str(exc_info.value) == message
    assert _errors(caplog) == [message]
    assert sent.calls == []


def test_killpg_refuses_its_own_group(sent: Recorder, caplog: pytest.LogCaptureFixture) -> None:
    message = (
        f"refusing to send SIGKILL to process group {os.getpgrp()}: it is this process's own group"
    )
    with pytest.raises(UnsafeSignalTarget) as exc_info:
        process_signals.killpg(os.getpgrp(), signal.SIGKILL)
    assert str(exc_info.value) == message
    assert _errors(caplog) == [message]
    assert sent.calls == []


def test_an_unnamed_signal_number_is_reported_by_number(
    sent: Recorder, caplog: pytest.LogCaptureFixture
) -> None:
    with pytest.raises(UnsafeSignalTarget):
        process_signals.kill(1, 200)
    assert _errors(caplog) == [f"refusing to send signal 200 to pid 1: {INIT}"]


def test_one_other_process_and_its_group_pass_through(sent: Recorder) -> None:
    process_signals.kill(4242, signal.SIGTERM)
    process_signals.killpg(4242, signal.SIGKILL)
    process_signals.signal_group_of(4243, signal.SIGTERM)
    assert sent.calls == [
        ("kill", 4242, signal.SIGTERM),
        ("killpg", 4242, signal.SIGKILL),
        ("getpgid", 4243),
        ("killpg", 4242, signal.SIGTERM),
    ]


def test_signal_group_of_checks_the_pid_before_asking_its_group(sent: Recorder) -> None:
    """``os.getpgid(0)`` is this process's own group."""
    with pytest.raises(UnsafeSignalTarget):
        process_signals.signal_group_of(0, signal.SIGTERM)
    assert sent.calls == []


@pytest.mark.parametrize("pgid", [0, 1])
def test_signal_group_of_refuses_the_group_of_a_kernel_thread_or_init(
    sent: Recorder, caplog: pytest.LogCaptureFixture, pgid: int
) -> None:
    """A recorded pid now held by a kernel thread belongs to group 0."""
    sent.pgid = pgid
    with pytest.raises(UnsafeSignalTarget):
        process_signals.signal_group_of(77, signal.SIGTERM)
    assert sent.calls == [("getpgid", 77)]
    assert len(_errors(caplog)) == 1


def test_signal_group_of_refuses_its_own_group(sent: Recorder) -> None:
    sent.pgid = os.getpgrp()
    with pytest.raises(UnsafeSignalTarget):
        process_signals.signal_group_of(4242, signal.SIGKILL)
    assert sent.calls == [("getpgid", 4242)]


def test_a_live_child_is_really_signalled(live_worker_pid: int) -> None:
    """End to end, unmocked: the guard lets a process this test started through."""
    process_signals.signal_group_of(live_worker_pid, signal.SIGKILL)
    _, status = os.waitpid(live_worker_pid, 0)
    assert os.WTERMSIG(status) == signal.SIGKILL


# ---------------------------------------------------------------------------
# Every signal in src goes through the guard
# ---------------------------------------------------------------------------


def _direct_signal_calls(source: str) -> list[int]:
    """Line numbers where ``source`` uses ``os.kill``/``os.killpg`` or imports them."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in {"kill", "killpg"}
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
        ) or (
            isinstance(node, ast.ImportFrom)
            and node.module == "os"
            and any(alias.name in {"kill", "killpg"} for alias in node.names)
        ):
            lines.append(node.lineno)
    return sorted(lines)


def test_the_scan_finds_every_form_of_direct_signal() -> None:
    source = (
        "import os\n"
        "os.kill(1, 9)\n"
        "send = os.killpg\n"
        "from os import killpg\n"
        "os.getpid()\n"
        "# os.kill(1, 9) in a comment\n"
    )
    assert _direct_signal_calls(source) == [2, 3, 4]


def test_nothing_in_src_signals_a_process_except_process_signals() -> None:
    root = Path(claude_task_runner.__file__).parent
    guard = root / "process_signals.py"
    offenders = [
        f"{path.relative_to(root)}:{line}"
        for path in sorted(root.rglob("*.py"))
        if path != guard
        for line in _direct_signal_calls(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


# ---------------------------------------------------------------------------
# The dispatcher's probes and kills
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pid", [1, 0, -1])
def test_pid_alive_reports_a_pid_of_one_or_less_dead(sent: Recorder, pid: int) -> None:
    """Probing pid 1 fails with EPERM, which read as a live worker."""
    assert dispatcher_mod._pid_alive(pid) is False
    assert sent.calls == []


def test_pid_alive_reports_its_own_pid_dead(sent: Recorder) -> None:
    assert dispatcher_mod._pid_alive(os.getpid()) is False
    assert sent.calls == []


def test_signal_group_by_pid_refuses_init(sent: Recorder) -> None:
    with pytest.raises(UnsafeSignalTarget):
        dispatcher_mod._signal_group_by_pid(1, signal.SIGTERM)
    assert sent.calls == []


def test_terminate_refuses_a_worker_in_its_own_group(sent: Recorder) -> None:
    """A worker started without its own session shares the supervisor's group."""
    sent.pgid = os.getpgrp()
    process = MagicMock(spec=subprocess.Popen)
    process.pid = 4242
    with pytest.raises(dispatcher_mod.TerminateFailed) as exc_info:
        dispatcher_mod._terminate(process)
    assert str(exc_info.value) == (
        f"task pid 4242: refusing to send SIGTERM to process group {os.getpgrp()}: "
        "it is this process's own group"
    )
    assert sent.calls == [("getpgid", 4242)]
    process.wait.assert_not_called()


def test_terminate_by_pid_refuses_before_sending_or_polling(sent: Recorder) -> None:
    alive = MagicMock(return_value=True)
    with pytest.raises(UnsafeSignalTarget):
        dispatcher_mod._terminate_by_pid(1, alive=alive, sleep_fn=lambda _s: None)
    alive.assert_not_called()
    assert sent.calls == []


# ---------------------------------------------------------------------------
# The reaper: the path that fired on 2026-09-26
# ---------------------------------------------------------------------------

LOOP_ARGV = "bash -c while ! [ -e /tmp/never_appears ]; do sleep 1; done"
NOW = datetime(2026, 9, 26, 19, 57, tzinfo=UTC)


def _fake_proc_with_a_loop_under_init(tmp_path: Path) -> Path:
    """A /proc where init's child is another run's marker-wait loop."""
    fake_proc = tmp_path / "proc"
    (fake_proc / "self").mkdir(parents=True)
    for pid, children, cmdline in [(1, [4242], "/sbin/init"), (4242, [], LOOP_ARGV)]:
        task_dir = fake_proc / str(pid) / "task" / str(pid)
        task_dir.mkdir(parents=True)
        (task_dir / "children").write_text(" ".join(str(c) for c in children))
        (fake_proc / str(pid) / "cmdline").write_bytes(
            cmdline.replace(" ", "\x00").encode() + b"\x00"
        )
    return fake_proc


def test_the_stuck_loop_scan_refuses_init_before_walking(
    sent: Recorder,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every process descends from pid 1, so the walk would match any loop."""
    monkeypatch.setattr(rs, "_PROC_ROOT", _fake_proc_with_a_loop_under_init(tmp_path))
    with pytest.raises(UnsafeSignalTarget):
        rs._detect_stuck_sleep_loop(1)
    assert _errors(caplog) == [f"refusing to scan the process tree of pid 1: {INIT}"]
    assert sent.calls == []


def test_default_sigterm_refuses_init(sent: Recorder) -> None:
    assert rs._default_sigterm(1) is False
    assert sent.calls == []


def _queue_with_a_pid_one_worker(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    write_task_atomic(Task(id="t-incident", title="t", prompt="p"), task_path_for(qd, "t-incident"))
    state = TaskState(
        task_id="t-incident",
        status="running",
        last_started_at=NOW - timedelta(seconds=3600),
        last_heartbeat_at=NOW - timedelta(seconds=2000),
        dispatcher_alive_at=NOW - timedelta(seconds=15),
        pid=1,
    )
    write_state_atomic(state, state_path_for(qd, "t-incident"))
    return qd


def _caps() -> TaskCapsSettings:
    return TaskCapsSettings(
        max_tokens_per_task=0,
        max_duration_s_per_task=0,
        heartbeat_silence_alert_s=300,
        heartbeat_silence_kill_s=900,
        zombie_verify_fs_activity_window_s=600,
        bash_poll_antipattern_kill=True,
        stuck_sleep_loop_kill_threshold_s=600,
    )


def test_incident_replay_scan_sends_nothing(
    sent: Recorder,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 2026-09-26 state: a pid-1 worker, heartbeat 2000 s stale, the
    dispatcher alive, and a marker-wait loop under init. Unguarded, the scan
    matched and the kill went to process group 1."""
    qd = _queue_with_a_pid_one_worker(tmp_path)
    monkeypatch.setattr(rs, "_PROC_ROOT", _fake_proc_with_a_loop_under_init(tmp_path))

    results = rs.reap_silent_orphans_tick(
        qd, {"t-incident"}, settings=_caps(), clock=FakeClock(NOW)
    )

    assert results == []
    assert load_state(state_path_for(qd, "t-incident")).status == "running"
    assert sent.calls == []
    assert _errors(caplog) == [f"refusing to scan the process tree of pid 1: {INIT}"]


def test_incident_replay_kill_sends_nothing(
    sent: Recorder, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """Had the scan matched anyway, the default terminate refuses pid 1 and
    the task is still demoted, with ``sigtermed`` false."""
    qd = _queue_with_a_pid_one_worker(tmp_path)

    results = rs.reap_silent_orphans_tick(
        qd,
        {"t-incident"},
        settings=_caps(),
        clock=FakeClock(NOW),
        stuck_loop_detect_fn=lambda _pid: (4242, LOOP_ARGV),
    )

    assert [(r.verdict, r.pid, r.sigtermed, r.stuck_loop_bash_pid) for r in results] == [
        (HeartbeatVerdict.KILL, 1, False, 4242)
    ]
    assert load_state(state_path_for(qd, "t-incident")).status == "failed"
    assert sent.calls == []
    assert _errors(caplog) == [f"refusing to send SIGTERM to pid 1: {INIT}"]


# ---------------------------------------------------------------------------
# supervisor stop / drain
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("command", "sig"), [("stop", "SIGTERM"), ("drain", "SIGUSR1")])
def test_a_pid_file_holding_one_is_stale_not_signalled(
    sent: Recorder, tmp_path: Path, command: str, sig: str
) -> None:
    from claude_task_runner.cli.supervisor_cmd import app

    (tmp_path / ".claude_task_runner").mkdir()
    (tmp_path / ".claude_task_runner" / "supervisor.pid").write_text("1\n")
    result = CliRunner().invoke(app, [command, "--queue", str(tmp_path)])
    assert result.exit_code == 1
    assert result.stdout == "PID 1 not alive (stale PID file)\n"
    assert sent.calls == []


@pytest.mark.parametrize(("command", "sig"), [("stop", "SIGTERM"), ("drain", "SIGUSR1")])
def test_the_signal_site_refuses_init_on_its_own(
    sent: Recorder, tmp_path: Path, command: str, sig: str
) -> None:
    """Even when the liveness probe is fooled, the signal itself is refused."""
    from claude_task_runner.cli.supervisor_cmd import app

    (tmp_path / ".claude_task_runner").mkdir()
    (tmp_path / ".claude_task_runner" / "supervisor.pid").write_text("1\n")
    with patch("claude_task_runner.cli.supervisor_cmd.pidfile_mod.is_pid_alive", return_value=True):
        result = CliRunner().invoke(app, [command, "--queue", str(tmp_path)])
    assert result.exit_code == 2
    assert result.stdout == f"refusing to send {sig} to pid 1: {INIT}\n"
    assert sent.calls == []
