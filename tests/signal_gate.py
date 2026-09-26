"""Pytest plugin: a test may signal only processes it started.

On 2026-09-26 the suite killed every process of the user running it, twice.
Reaper tests borrowed pid 1 as a live worker. The stuck-sleep-loop scan
walked pid 1's process tree, which is every process on the machine, matched
another run's marker-wait loop, and the kill path sent SIGTERM and SIGKILL
to process group 1, which glibc turns into ``kill(-1)``. ``src`` now refuses
such targets itself (:mod:`claude_task_runner.process_signals`). This plugin
is the second layer, for tests.

For the whole of every test, :func:`signal_gate` replaces ``os.kill`` and
``os.killpg`` and blocks a non-zero signal that would reach a process the
test did not start:

* a pid of 1 or less, or a process group of 1 or less;
* the test process itself or its group, unless the test is marked
  ``allow_self_signal``;
* any other process that is not a descendant of the test process. For a
  group that is its leader, or every member once the leader has exited.

A blocked call sends nothing and raises :class:`SignalGateError`, and the
test fails at teardown even if the code under test swallowed the error.
Signal-0 probes pass through.

The gate also fails a test whose code runs the reaper's stuck-sleep-loop
scan over the real ``/proc`` from a pid the test did not start, since a
match there leads straight to a kill. A test that seeds a borrowed pid must
inject ``stuck_loop_detect_fn``, ``terminate_fn`` and ``sigterm_fn``, or set
``bash_poll_antipattern_kill`` to false. A test that needs a live worker
uses :func:`live_worker_pid`.
"""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from claude_task_runner.supervisor import reconcile_silent

_PROC = Path("/proc")


class SignalGateError(AssertionError):
    """A test tried to signal a process it did not start. Nothing was sent."""


def _stat_after_comm(pid: int) -> list[str] | None:
    """``/proc/<pid>/stat`` fields after the command name, or ``None`` if ``pid`` is gone.

    ``[1]`` is the parent pid and ``[2]`` the process group.
    """
    try:
        text = (_PROC / str(pid) / "stat").read_text()
    except OSError:
        return None
    # The command name is parenthesised and may itself hold spaces or ")".
    return text[text.rindex(")") + 2 :].split()


def is_descendant(pid: int, ancestor: int) -> bool:
    """True iff ``ancestor`` is on ``pid``'s chain of parents (``pid`` itself excluded)."""
    current = pid
    for _ in range(512):
        fields = _stat_after_comm(current)
        if fields is None:
            return False
        parent = int(fields[1])
        if parent == ancestor:
            return True
        if parent <= 1:
            return False
        current = parent
    return False


def _group_members(pgid: int) -> list[int]:
    members: list[int] = []
    for entry in _PROC.iterdir():
        if entry.name.isdigit():
            fields = _stat_after_comm(int(entry.name))
            if fields is not None and int(fields[2]) == pgid:
                members.append(int(entry.name))
    return members


def _signal_name(sig: int) -> str:
    try:
        return signal.Signals(sig).name
    except ValueError:
        return str(sig)


class SignalGate:
    """``os.kill`` and ``os.killpg`` stand-ins that block other processes' signals.

    ``real_kill`` and ``real_killpg`` receive the calls the gate lets
    through. See the module docstring for what it blocks.
    """

    def __init__(
        self,
        real_kill: Callable[[int, int], None],
        real_killpg: Callable[[int, int], None],
        *,
        allow_self: bool = False,
    ) -> None:
        self._real_kill = real_kill
        self._real_killpg = real_killpg
        self._allow_self = allow_self
        self._test_pid = os.getpid()
        self._started: set[int] = set()
        self.violations: list[str] = []

    def allow(self, pid: int) -> None:
        """Vouch for ``pid`` (and a group it leads) as started by this test.

        For a process the test started that no longer descends from it,
        such as a worker a launcher double-forked so that init adopted it.
        """
        self._started.add(pid)

    def kill(self, pid: int, sig: int) -> None:
        if sig != 0:
            reason = self._pid_reason(pid)
            if reason is not None:
                self._block(f"os.kill({pid}, {_signal_name(sig)}) targets {reason}")
        self._real_kill(pid, sig)

    def killpg(self, pgid: int, sig: int) -> None:
        if sig != 0:
            reason = self._group_reason(pgid)
            if reason is not None:
                self._block(f"os.killpg({pgid}, {_signal_name(sig)}) targets {reason}")
        self._real_killpg(pgid, sig)

    def check_scan_root(self, pid: int) -> None:
        """Block a stuck-sleep-loop scan of the real ``/proc`` from a pid this test did not start."""
        if not self._started_by_test(pid):
            self._block(
                f"the stuck-sleep-loop scan walked the real /proc from pid {pid}, which this "
                "test did not start; inject stuck_loop_detect_fn, terminate_fn and "
                "sigterm_fn, or set bash_poll_antipattern_kill=False"
            )

    def _started_by_test(self, pid: int) -> bool:
        return pid in self._started or is_descendant(pid, self._test_pid)

    def _pid_reason(self, pid: int) -> str | None:
        if pid == self._test_pid:
            if self._allow_self:
                return None
            return "the test process itself (mark the test allow_self_signal if that is the point)"
        if pid == -1:
            return "every process the user owns"
        if pid <= 0:
            return "a whole process group"
        if pid == 1:
            return "init"
        if _stat_after_comm(pid) is not None and not self._started_by_test(pid):
            return "a process this test did not start"
        return None

    def _group_reason(self, pgid: int) -> str | None:
        if pgid == 1:
            return "every process the user owns (killpg(1) is kill(-1))"
        if pgid == 0 or pgid == os.getpgrp():
            return "the test process's own group"
        if pgid < 0:
            return "a negative process group"
        if _stat_after_comm(pgid) is not None:
            if self._started_by_test(pgid):
                return None
            return "a group this test did not start"
        strangers = [m for m in _group_members(pgid) if not self._started_by_test(m)]
        if strangers:
            return f"a group holding processes this test did not start: {strangers[:5]}"
        return None

    def _block(self, message: str) -> None:
        self.violations.append(message)
        raise SignalGateError(message)


@pytest.fixture(autouse=True)
def signal_gate(request: pytest.FixtureRequest) -> Iterator[SignalGate]:
    """Install a :class:`SignalGate` for the test and fail it on any blocked call.

    It patches with a private ``MonkeyPatch``. Requesting the shared
    ``monkeypatch`` fixture from an autouse fixture would hold back a test's
    own patches until after other autouse fixtures' teardown, such as the
    conftest's check for leaked signal handlers.
    """
    gate = SignalGate(
        os.kill,
        os.killpg,
        allow_self=request.node.get_closest_marker("allow_self_signal") is not None,
    )
    real_detect = reconcile_silent._detect_stuck_sleep_loop

    def gated_detect(pid: int, *, max_descendants: int = 256) -> tuple[int, str] | None:
        if reconcile_silent._PROC_ROOT == _PROC:
            gate.check_scan_root(pid)
        return real_detect(pid, max_descendants=max_descendants)

    with pytest.MonkeyPatch.context() as patches:
        patches.setattr(os, "kill", gate.kill)
        patches.setattr(os, "killpg", gate.killpg)
        patches.setattr(reconcile_silent, "_detect_stuck_sleep_loop", gated_detect)
        yield gate
    if gate.violations:
        pytest.fail("signal gate: " + "; ".join(gate.violations), pytrace=False)


@pytest.fixture
def live_worker_pid() -> Iterator[int]:
    """The pid of a live, idle process this test started, to stand in for a worker.

    Never borrow a pid the test did not start, such as 1: the code under test
    may signal it, and pid 1's group is every process the user owns. This
    child runs ``sleep`` in a session of its own, so a signal to its group
    reaches nothing else. It is killed and reaped at teardown.
    """
    child = subprocess.Popen(["sleep", "600"], start_new_session=True)
    try:
        yield child.pid
    finally:
        child.kill()
        child.wait(timeout=10)
