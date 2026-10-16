"""``worker_probe_fn`` stubs for the silent-orphan reaper's tests.

The reapers ask :func:`runner.dispatcher.probe_worker` whether a recorded
pid is still its worker before they signal it or set its task ``failed``.
Tests record made-up pids, so they say what the probe finds instead, or
build a fake ``/proc`` for the real probe to read.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from claude_task_runner.queue.schema import TaskState
from claude_task_runner.runner.dispatcher import WorkerProbe


def worker_alive(_state: TaskState) -> WorkerProbe:
    """The recorded worker is still running."""
    return WorkerProbe.ALIVE


def worker_gone(_state: TaskState) -> WorkerProbe:
    """No process has the recorded pid any more."""
    return WorkerProbe.DEAD


def probes(*answers: WorkerProbe) -> Callable[[TaskState], WorkerProbe]:
    """A probe giving ``answers`` in turn, then the last one for every later call."""
    remaining = list(answers)

    def probe(_state: TaskState) -> WorkerProbe:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return probe


def ticks_between(boot: datetime, started: datetime) -> int:
    """``/proc/<pid>/stat`` starttime of a process started at ``started`` in a
    boot that began at ``boot``."""
    return round((started - boot).total_seconds() * os.sysconf("SC_CLK_TCK"))


def fake_proc(
    root: Path,
    pid: int,
    *,
    started: datetime,
    boot: datetime | None,
    stdout: Path | None = None,
    state: str = "S",
    with_stat: bool = True,
    boot_id: str | None = "boot-a",
) -> Path:
    """Write a ``/proc`` at ``root`` holding one process, ``pid``; return ``root``.

    The process started at ``started`` in a boot that began at ``boot``
    (``None`` leaves out the ``btime`` line) and whose id is ``boot_id``
    (``None`` leaves out ``sys/kernel/random/boot_id``). Its command name
    holds a space and a ``)``, as ``/proc/<pid>/stat`` allows. ``stdout``,
    when given, is where its fd 1 points; otherwise fd 1 cannot be read, as
    for another user's process. ``with_stat=False`` leaves out
    ``/proc/<pid>/stat``, as ``hidepid`` hides another user's process.
    """
    (root / str(pid) / "fd").mkdir(parents=True)
    btime = f"btime {int(boot.timestamp())}\n" if boot is not None else ""
    (root / "stat").write_text(f"cpu  1 2 3 4\n{btime}processes 99\n")
    if boot_id is not None:
        (root / "sys" / "kernel" / "random").mkdir(parents=True)
        (root / "sys" / "kernel" / "random" / "boot_id").write_text(boot_id + "\n")
    if with_stat:
        ticks = ticks_between(boot if boot is not None else started, started)
        fields = [state, *(["0"] * 18), str(ticks), *(["0"] * 30)]
        (root / str(pid) / "stat").write_text(f"{pid} (claude (x) y) {' '.join(fields)}\n")
    if stdout is not None:
        (root / str(pid) / "fd" / "1").symlink_to(stdout)
    return root
