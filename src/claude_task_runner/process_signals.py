"""The one place the runner sends a signal to a process.

``os.kill`` and ``os.killpg`` take numbers that can mean far more than one
process. ``os.kill(0, sig)`` and ``os.killpg(0, sig)`` signal the caller's
own process group. ``os.kill(-1, sig)`` signals every process the user may
signal, and so does ``os.killpg(1, sig)``, because glibc implements
``killpg(pgrp)`` as ``kill(-pgrp)``. A wrong recorded pid reaches those
numbers easily: a state YAML with ``pid: 1``, a test that borrows pid 1 as
a live worker, or a pid now held by a kernel thread, whose process group
is 0. On 2026-09-26 the test suite sent SIGTERM and then SIGKILL to process
group 1 this way, twice, and killed every process of the user running it.

So every signal the runner sends goes through :func:`kill`,
:func:`killpg` or :func:`signal_group_of`. They refuse a pid or process
group of 1 or less, and this process's own pid or group, before sending
anything: a refusal logs at ERROR and raises :class:`UnsafeSignalTarget`.
Liveness probes (signal 0) go through :func:`kill` as well, so a probe of
pid 1 does not report init as a live worker.

``tests/unit/test_process_signals.py`` fails if code under ``src`` calls
``os.kill`` or ``os.killpg`` anywhere else.
"""

from __future__ import annotations

import logging
import os
import signal

logger = logging.getLogger(__name__)


class UnsafeSignalTarget(ValueError):
    """A signal was about to go to a pid or process group the runner must never signal.

    Raised before anything is sent. The message says what was refused and
    why, and the refusal has already been logged at ERROR.
    """


def _pid_hazard(pid: int) -> str | None:
    """Why ``pid`` must never be signalled, or ``None`` when it is one other process."""
    if pid == 0:
        return "pid 0 means this process's own process group"
    if pid == 1:
        return "pid 1 is init, and process group 1 is every process the user owns"
    if pid < 0:
        return "a negative pid is a process group, and -1 is every process the user owns"
    if pid == os.getpid():
        return "it is this process"
    return None


def _pgid_hazard(pgid: int) -> str | None:
    """Why process group ``pgid`` must never be signalled, or ``None``."""
    if pgid == 0:
        return "process group 0 means this process's own group"
    if pgid == 1:
        return "killpg(1) is kill(-1), which signals every process the user owns"
    if pgid < 0:
        return "a process group id is never negative"
    if pgid == os.getpgrp():
        return "it is this process's own group"
    return None


def _action(sig: int) -> str:
    """``"probe"`` for signal 0, else ``"send SIGTERM to"`` and the like."""
    if sig == 0:
        return "probe"
    try:
        return f"send {signal.Signals(sig).name} to"
    except ValueError:
        return f"send signal {sig} to"


def _refuse(action: str, target: str, hazard: str) -> UnsafeSignalTarget:
    message = f"refusing to {action} {target}: {hazard}"
    logger.error(message)
    return UnsafeSignalTarget(message)


def refuse_unsafe_pid(pid: int, action: str) -> None:
    """Raise :class:`UnsafeSignalTarget` when ``pid`` is one the runner must never signal.

    ``action`` completes "refusing to <action> pid <pid>" in the message,
    such as ``"scan the process tree of"`` for a walk whose match leads to
    a signal.
    """
    hazard = _pid_hazard(pid)
    if hazard is not None:
        raise _refuse(action, f"pid {pid}", hazard)


def kill(pid: int, sig: int) -> None:
    """``os.kill(pid, sig)``, refusing a pid of 1 or less and this process's own.

    Raises :class:`UnsafeSignalTarget` without sending anything on a
    refusal; otherwise raises whatever ``os.kill`` raises.
    """
    refuse_unsafe_pid(pid, _action(sig))
    os.kill(pid, sig)


def killpg(pgid: int, sig: int) -> None:
    """``os.killpg(pgid, sig)``, refusing a group of 1 or less and this process's own.

    Raises :class:`UnsafeSignalTarget` without sending anything on a
    refusal; otherwise raises whatever ``os.killpg`` raises.
    """
    hazard = _pgid_hazard(pgid)
    if hazard is not None:
        raise _refuse(_action(sig), f"process group {pgid}", hazard)
    os.killpg(pgid, sig)


def signal_group_of(pid: int, sig: int) -> None:
    """Send ``sig`` to the process group ``pid`` belongs to.

    Checks ``pid`` before asking for its group, since ``os.getpgid(0)`` is
    this process's group, and then checks the group: a pid now held by a
    kernel thread belongs to group 0. Raises :class:`UnsafeSignalTarget`
    on a refusal and :class:`ProcessLookupError` when ``pid`` is gone.
    """
    refuse_unsafe_pid(pid, _action(sig))
    killpg(os.getpgid(pid), sig)
