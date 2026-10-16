"""Tests for :func:`runner.dispatcher.probe_worker`, the worker-identity probe.

``_pid_alive`` says only that some process has a pid. ``probe_worker``
also says whether that process is still the worker a task's state
recorded, so a pid the kernel gave to another process after the worker
exited (after a reboot, say) is neither adopted, nor signalled, nor kept
``running``.

The first group builds a fake ``/proc`` so each check is pinned with
exact values; the second runs real processes against the host's ``/proc``.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from claude_task_runner.runner import dispatcher as dispatcher_mod
from claude_task_runner.runner.dispatcher import (
    WORKER_START_SLACK_AFTER_S,
    WORKER_START_SLACK_BEFORE_S,
    WorkerProbe,
    probe_worker,
)

from ._worker_probe_stubs import fake_proc

_PID = 4242
_BOOT = datetime(2026, 10, 1, tzinfo=UTC)
_STARTED = datetime(2026, 10, 16, 13, 11, 19, tzinfo=UTC)
"""The attempt's ``last_started_at``."""


def _fake_proc(
    tmp_path: Path,
    *,
    state: str = "S",
    started: datetime = _STARTED,
    stdout: Path | None = None,
    boot: datetime | None = _BOOT,
    with_stat: bool = True,
) -> Path:
    """A fake ``/proc`` holding ``_PID`` (see :func:`._worker_probe_stubs.fake_proc`)."""
    return fake_proc(
        tmp_path / "proc",
        _PID,
        started=started,
        boot=boot,
        stdout=stdout,
        state=state,
        with_stat=with_stat,
    )


@pytest.fixture
def alive(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_PID`` exists, as far as signal 0 can tell."""
    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda pid: pid == _PID)


@pytest.fixture
def log(tmp_path: Path) -> Path:
    path = tmp_path / "attempt-3.stream.jsonl"
    path.write_text('{"type":"system","subtype":"init","session_id":"s"}\n')
    return path


def _use(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr(dispatcher_mod, "_PROC_ROOT", root)


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


# ---------------------------------------------------------------------------
# Fake /proc: each check with exact values
# ---------------------------------------------------------------------------


def test_no_process_is_dead(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, log: Path) -> None:
    monkeypatch.setattr(dispatcher_mod, "_pid_alive", lambda _pid: False)
    _use(monkeypatch, _fake_proc(tmp_path, stdout=log))
    assert probe_worker(_PID, log_path=str(log), started_at=_STARTED) is WorkerProbe.DEAD


@pytest.mark.usefixtures("alive")
def test_without_proc_existence_decides(log: Path) -> None:
    """The autouse fixture points ``_PROC_ROOT`` at a path that does not
    exist, as on a host without ``/proc``: only the pid's existence is known."""
    assert probe_worker(_PID, log_path=str(log), started_at=_STARTED) is WorkerProbe.ALIVE


@pytest.mark.usefixtures("alive")
@pytest.mark.parametrize("state", ["Z", "X"])
def test_zombie_is_dead(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, log: Path, state: str
) -> None:
    """Signal 0 succeeds on a zombie, which has exited."""
    _use(monkeypatch, _fake_proc(tmp_path, state=state, stdout=log))
    assert probe_worker(_PID, log_path=str(log), started_at=_STARTED) is WorkerProbe.DEAD


@pytest.mark.usefixtures("alive")
def test_stdout_is_the_log_is_alive_whatever_the_start_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, log: Path
) -> None:
    """The fd check settles it: a start time far outside the window does
    not override a stdout that is the attempt's own log."""
    _use(monkeypatch, _fake_proc(tmp_path, stdout=log, started=_STARTED + timedelta(hours=5)))
    assert probe_worker(_PID, log_path=str(log), started_at=_STARTED) is WorkerProbe.ALIVE


@pytest.mark.usefixtures("alive")
def test_stdout_is_another_file_is_reused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    log: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A process whose stdout is not the log is not the worker, even when
    it started when the attempt did."""
    other = tmp_path / "other.out"
    other.write_text("")
    _use(monkeypatch, _fake_proc(tmp_path, stdout=other))
    with caplog.at_level(logging.WARNING):
        assert probe_worker(_PID, log_path=str(log), started_at=_STARTED) is WorkerProbe.REUSED
    assert _warnings(caplog) == [
        f"pid {_PID} is not the worker that wrote {log}: its stdout is another file, "
        "so the worker exited and its pid now belongs to another process"
    ]


@pytest.mark.usefixtures("alive")
@pytest.mark.parametrize(
    ("offset_s", "expected"),
    [
        (0.0, WorkerProbe.ALIVE),
        (-WORKER_START_SLACK_BEFORE_S + 1, WorkerProbe.ALIVE),
        (-WORKER_START_SLACK_BEFORE_S - 1, WorkerProbe.REUSED),
        (WORKER_START_SLACK_AFTER_S - 1, WorkerProbe.ALIVE),
        (WORKER_START_SLACK_AFTER_S + 1, WorkerProbe.REUSED),
    ],
)
def test_unreadable_stdout_falls_back_to_the_start_time(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    log: Path,
    offset_s: float,
    expected: WorkerProbe,
) -> None:
    """Another user's worker (``sudo``) has an unreadable fd table: its
    process must have started within the window around ``last_started_at``."""
    _use(monkeypatch, _fake_proc(tmp_path, started=_STARTED + timedelta(seconds=offset_s)))
    assert probe_worker(_PID, log_path=str(log), started_at=_STARTED) is expected


@pytest.mark.usefixtures("alive")
def test_start_time_mismatch_is_logged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _use(monkeypatch, _fake_proc(tmp_path, started=_STARTED + timedelta(seconds=400)))
    with caplog.at_level(logging.WARNING):
        assert probe_worker(_PID, log_path=None, started_at=_STARTED) is WorkerProbe.REUSED
    assert _warnings(caplog) == [
        f"pid {_PID} is not the worker of the attempt started at 2026-10-16T13:11:19+00:00 "
        "(log None): the process started +400s from the attempt's start, so the worker "
        "exited and its pid now belongs to another process"
    ]


@pytest.mark.usefixtures("alive")
def test_a_boot_after_the_attempt_started_means_reused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The worker died with the previous boot; whatever has its pid now
    started after this boot, however close that is to the attempt."""
    boot = _STARTED + timedelta(seconds=30)
    _use(monkeypatch, _fake_proc(tmp_path, boot=boot, started=boot + timedelta(seconds=10)))
    with caplog.at_level(logging.WARNING):
        assert probe_worker(_PID, log_path=None, started_at=_STARTED) is WorkerProbe.REUSED
    assert _warnings(caplog) == [
        f"pid {_PID} is not the worker of the attempt started at 2026-10-16T13:11:19+00:00 "
        "(log None): this boot began at 2026-10-16T13:11:49+00:00, after the attempt "
        "started, so the worker exited and its pid now belongs to another process"
    ]


@pytest.mark.usefixtures("alive")
def test_hidden_process_falls_back_to_existence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, log: Path
) -> None:
    """With ``hidepid``, another user's ``/proc/<pid>`` is not there although
    signal 0 finds the process: only existence is known."""
    _use(monkeypatch, _fake_proc(tmp_path, with_stat=False))
    assert probe_worker(_PID, log_path=str(log), started_at=_STARTED) is WorkerProbe.ALIVE


@pytest.mark.usefixtures("alive")
def test_unreadable_boot_time_skips_the_start_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, log: Path
) -> None:
    _use(monkeypatch, _fake_proc(tmp_path, boot=None, started=_STARTED + timedelta(days=3)))
    assert probe_worker(_PID, log_path=str(log), started_at=_STARTED) is WorkerProbe.ALIVE


@pytest.mark.usefixtures("alive")
def test_without_a_start_time_or_readable_stdout_existence_decides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _use(monkeypatch, _fake_proc(tmp_path, started=_STARTED + timedelta(days=3)))
    assert probe_worker(_PID, log_path=None, started_at=None) is WorkerProbe.ALIVE


def test_pid_one_is_refused_and_dead() -> None:
    """Init is never a worker; the probe refuses it before reading anything."""
    assert probe_worker(1, log_path=None, started_at=None) is WorkerProbe.DEAD


# ---------------------------------------------------------------------------
# Real processes on the host's /proc
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("real_proc")
def test_real_worker_writing_its_log_is_alive_until_it_exits(log: Path, tmp_path: Path) -> None:
    """A process started with the log as stdout, as the dispatcher starts a
    worker, is ALIVE for that log and REUSED for another; once it has exited
    and been reaped it is DEAD, and its zombie before that is DEAD too."""
    started_at = datetime.now(UTC)
    with open(log, "ab") as out:
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=out,
            start_new_session=True,
        )
    try:
        other = tmp_path / "other.jsonl"
        other.write_text("")
        assert probe_worker(child.pid, log_path=str(log), started_at=started_at) is (
            WorkerProbe.ALIVE
        )
        assert probe_worker(child.pid, log_path=str(other), started_at=started_at) is (
            WorkerProbe.REUSED
        )
        # No log to compare: the start time decides.
        assert probe_worker(child.pid, log_path=None, started_at=started_at) is WorkerProbe.ALIVE
        assert probe_worker(
            child.pid, log_path=None, started_at=started_at - timedelta(hours=1)
        ) is (WorkerProbe.REUSED)
        child.kill()
        # Not waited for yet: a zombie, which signal 0 still finds.
        deadline = datetime.now(UTC) + timedelta(seconds=10)
        while Path(f"/proc/{child.pid}/stat").read_text().split(")")[-1].split()[0] != "Z":
            assert datetime.now(UTC) < deadline, "child never became a zombie"
        assert dispatcher_mod._pid_alive(child.pid) is True
        assert probe_worker(child.pid, log_path=str(log), started_at=started_at) is (
            WorkerProbe.DEAD
        )
    finally:
        child.kill()
        child.wait(timeout=10)
    assert probe_worker(child.pid, log_path=str(log), started_at=started_at) is WorkerProbe.DEAD
