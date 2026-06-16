"""Tests for the dispatcher's process-group termination path.

``_terminate`` is the cap-kill / silence-kill escalation. After
ADR-0021 it signals the subprocess's whole process group rather than
just the parent pid, so children ``claude`` forked (MCP servers,
shelled-out ``git`` / ``Rscript``) are reaped too. The subprocess is
spawned with ``start_new_session=True`` precisely so the group exists
to be signalled.

The 2026-06-13 ``frompeople-903-farrell_2013`` zombie post-mortem
strengthened the contract further: ``_terminate`` now VERIFIES the
parent actually exited via a post-SIGKILL ``process.wait`` and RAISES
:class:`TerminateFailed` when the kill can't be confirmed (signal-send
OSError on SIGKILL, or parent surviving SIGKILL). A raised
``TerminateFailed`` propagates out of the dispatch loop so the
dispatcher does not finalize the slot to ``failed`` and clear the pid
— the on-disk state stays ``"running"`` for the per-tick silent-orphan
reaper to pick up. Letting the dispatcher pretend the kill succeeded
is the bug that lost 30+ hours of slot capacity in the live incident.

These tests cover both the mocked unit paths (one ``_terminate``
invocation, patched ``os.killpg`` / ``os.getpgid``) and integration
paths that spawn real Bash subprocesses to prove the PG-wide signal
actually reaps SIGTERM-ignoring children + grandchildren in a live
process tree.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from unittest import mock

import pytest

from claude_task_runner.runner import dispatcher as dispatcher_mod
from claude_task_runner.runner.dispatcher import (
    TerminateFailed,
    _signal_group,
    _terminate,
)


class _FakeProcess:
    """Minimal ``subprocess.Popen`` shape ``_terminate`` touches.

    ``wait_outcomes`` is a list of side-effects consumed in order on
    successive ``wait()`` calls — each entry is either an ``int``
    (return that exit code) or an exception class (raise it). This lets
    a single test express "SIGTERM grace wait times out, SIGKILL
    verification wait succeeds" or vice versa.

    The real Popen object's ``send_signal`` / ``kill`` are NOT used by
    ``_terminate`` (it goes through ``os.killpg`` directly), so they're
    absent on purpose — a regression that reintroduces a pid-only
    ``process.kill()`` would ``AttributeError`` here and fail loudly.
    """

    def __init__(
        self,
        *,
        pid: int = 4242,
        wait_outcomes: list[int | type[BaseException]] | None = None,
    ) -> None:
        self.pid = pid
        self._outcomes = list(wait_outcomes or [0])
        self.wait_calls: list[float | None] = []

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        if not self._outcomes:
            return 0
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, type) and issubclass(outcome, BaseException):
            if outcome is subprocess.TimeoutExpired:
                raise subprocess.TimeoutExpired(cmd="claude", timeout=timeout or 0)
            raise outcome()
        assert isinstance(outcome, int)
        return outcome


# --- Mocked unit tests ----------------------------------------------------


def test_terminate_sigterms_group_then_no_kill_on_clean_wait() -> None:
    """A subprocess that exits within the 5s grace period gets exactly one
    SIGTERM to its group and no SIGKILL. The verification wait is NOT
    invoked when SIGTERM already worked (the second wait is only for
    the SIGKILL escalation path)."""
    process = _FakeProcess(pid=1000, wait_outcomes=[0])

    with (
        mock.patch.object(dispatcher_mod.os, "getpgid", return_value=1000) as getpgid,
        mock.patch.object(dispatcher_mod.os, "killpg") as killpg,
    ):
        _terminate(process)  # type: ignore[arg-type]

    getpgid.assert_called_once_with(1000)
    killpg.assert_called_once_with(1000, signal.SIGTERM)
    assert process.wait_calls == [5]


def test_terminate_escalates_sigterm_then_sigkill_then_verifies() -> None:
    """When SIGTERM's 5s grace wait times out, escalate to SIGKILL on the
    PG and verify with a 2s wait. Both signals target the pgid."""
    process = _FakeProcess(
        pid=2000,
        wait_outcomes=[subprocess.TimeoutExpired, 0],
    )

    with (
        mock.patch.object(dispatcher_mod.os, "getpgid", return_value=2000),
        mock.patch.object(dispatcher_mod.os, "killpg") as killpg,
    ):
        _terminate(process)  # type: ignore[arg-type]

    assert killpg.call_args_list == [
        mock.call(2000, signal.SIGTERM),
        mock.call(2000, signal.SIGKILL),
    ]
    assert process.wait_calls == [5, 2]


def test_terminate_returns_silently_when_parent_already_gone() -> None:
    """``getpgid`` ``ProcessLookupError`` ⇒ parent is already dead; return
    cleanly with no signals sent."""
    process = _FakeProcess(pid=3000)

    with (
        mock.patch.object(dispatcher_mod.os, "getpgid", side_effect=ProcessLookupError),
        mock.patch.object(dispatcher_mod.os, "killpg") as killpg,
    ):
        _terminate(process)  # type: ignore[arg-type]

    killpg.assert_not_called()
    assert process.wait_calls == []


def test_terminate_returns_silently_when_group_vanished_on_sigterm() -> None:
    """``killpg(SIGTERM)`` ``ProcessLookupError`` (the group raced its own
    exit between getpgid and killpg) ⇒ return cleanly."""
    process = _FakeProcess(pid=3100)

    with (
        mock.patch.object(dispatcher_mod.os, "getpgid", return_value=3100),
        mock.patch.object(dispatcher_mod.os, "killpg", side_effect=ProcessLookupError) as killpg,
    ):
        _terminate(process)  # type: ignore[arg-type]

    killpg.assert_called_once_with(3100, signal.SIGTERM)
    assert process.wait_calls == []


def test_terminate_warns_on_sigterm_oserror_and_falls_through_to_sigkill(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A non-vanished ``OSError`` on the SIGTERM call is logged at WARNING
    and the function falls through to the SIGKILL escalation rather
    than raising. The transient SIGTERM-send failure does not by itself
    prove the kill chain has failed — SIGKILL gets the next try."""
    process = _FakeProcess(
        pid=4100,
        wait_outcomes=[subprocess.TimeoutExpired, 0],
    )

    def killpg_side_effect(_pgid: int, sig: int) -> None:
        if sig == signal.SIGTERM:
            raise PermissionError("operation not permitted")
        # SIGKILL succeeds.

    with (
        mock.patch.object(dispatcher_mod.os, "getpgid", return_value=4100),
        mock.patch.object(dispatcher_mod.os, "killpg", side_effect=killpg_side_effect),
        caplog.at_level(logging.WARNING, logger="claude_task_runner.runner.dispatcher"),
    ):
        _terminate(process)  # type: ignore[arg-type]

    warn = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warn, "expected a WARNING log for the SIGTERM EPERM"
    msg = warn[0].getMessage()
    assert "SIGTERM to PG 4100" in msg
    assert "falling through to SIGKILL" in msg
    assert process.wait_calls == [5, 2]


def test_terminate_raises_terminate_failed_on_sigkill_oserror(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the escalation ``killpg(SIGKILL)`` raises a non-vanished
    ``OSError``, ``_terminate`` logs ERROR and raises
    :class:`TerminateFailed`. The caller must NOT mark the slot free —
    the subprocess may still be alive."""
    process = _FakeProcess(
        pid=5100,
        wait_outcomes=[subprocess.TimeoutExpired],
    )

    def killpg_side_effect(_pgid: int, sig: int) -> None:
        if sig == signal.SIGKILL:
            raise PermissionError("operation not permitted")

    with (
        mock.patch.object(dispatcher_mod.os, "getpgid", return_value=5100),
        mock.patch.object(dispatcher_mod.os, "killpg", side_effect=killpg_side_effect),
        caplog.at_level(logging.ERROR, logger="claude_task_runner.runner.dispatcher"),
        pytest.raises(TerminateFailed) as exc_info,
    ):
        _terminate(process)  # type: ignore[arg-type]

    assert "SIGKILL to PG 5100" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, PermissionError)
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors, "expected an ERROR log for the SIGKILL EPERM"
    msg = errors[0].getMessage()
    assert "SIGKILL to PG 5100" in msg
    assert "silent reaper" in msg


def test_terminate_returns_silently_when_group_vanished_on_sigkill() -> None:
    """Between the SIGTERM-wait timeout and the SIGKILL the parent could
    exit on its own (slow SIGTERM handler that finally completed). A
    ``ProcessLookupError`` on the SIGKILL is benign — return cleanly,
    no raise."""
    process = _FakeProcess(
        pid=5200,
        wait_outcomes=[subprocess.TimeoutExpired],
    )

    def killpg_side_effect(_pgid: int, sig: int) -> None:
        if sig == signal.SIGKILL:
            raise ProcessLookupError

    with (
        mock.patch.object(dispatcher_mod.os, "getpgid", return_value=5200),
        mock.patch.object(dispatcher_mod.os, "killpg", side_effect=killpg_side_effect),
    ):
        _terminate(process)  # type: ignore[arg-type]

    # SIGTERM wait timed out; SIGKILL hit ProcessLookupError, so we
    # returned without invoking the post-kill wait.
    assert process.wait_calls == [5]


def test_terminate_raises_terminate_failed_when_parent_survives_sigkill(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """SIGKILL is uncatchable; a post-SIGKILL ``wait(timeout=2)`` that
    itself times out means the kernel didn't reap the parent (e.g.
    TASK_UNINTERRUPTIBLE on a hung syscall). Log ERROR, raise
    :class:`TerminateFailed`."""
    process = _FakeProcess(
        pid=6100,
        wait_outcomes=[subprocess.TimeoutExpired, subprocess.TimeoutExpired],
    )

    with (
        mock.patch.object(dispatcher_mod.os, "getpgid", return_value=6100),
        mock.patch.object(dispatcher_mod.os, "killpg") as killpg,
        caplog.at_level(logging.ERROR, logger="claude_task_runner.runner.dispatcher"),
        pytest.raises(TerminateFailed) as exc_info,
    ):
        _terminate(process)  # type: ignore[arg-type]

    assert "survived SIGKILL" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, subprocess.TimeoutExpired)
    # Both signals fired in order before we gave up.
    assert killpg.call_args_list == [
        mock.call(6100, signal.SIGTERM),
        mock.call(6100, signal.SIGKILL),
    ]
    assert process.wait_calls == [5, 2]
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors, "expected an ERROR log for the survived-SIGKILL case"
    assert "TASK_UNINTERRUPTIBLE" in errors[0].getMessage()


# --- _signal_group passthrough tests (unchanged contract) -----------------


def test_signal_group_swallows_process_lookup_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A group that already vanished (``ProcessLookupError``) is benign at
    the ``_signal_group`` layer — no log, no raise."""
    process = _FakeProcess(pid=3000)

    with (
        mock.patch.object(dispatcher_mod.os, "getpgid", return_value=3000),
        mock.patch.object(
            dispatcher_mod.os, "killpg", side_effect=ProcessLookupError("no such process")
        ),
        caplog.at_level(logging.DEBUG, logger="claude_task_runner.runner.dispatcher"),
    ):
        _signal_group(process, signal.SIGTERM)  # type: ignore[arg-type]

    assert caplog.records == []


def test_signal_group_logs_other_oserror(caplog: pytest.LogCaptureFixture) -> None:
    """A non-vanished ``OSError`` (e.g. EPERM) on signal-send is logged at
    ERROR with the pid, never silently swallowed."""
    process = _FakeProcess(pid=5005)

    with (
        mock.patch.object(dispatcher_mod.os, "getpgid", return_value=5005),
        mock.patch.object(
            dispatcher_mod.os, "killpg", side_effect=PermissionError("operation not permitted")
        ),
        caplog.at_level(logging.ERROR, logger="claude_task_runner.runner.dispatcher"),
    ):
        _signal_group(process, signal.SIGTERM)  # type: ignore[arg-type]

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.ERROR
    msg = record.getMessage()
    assert "terminate signal failed" in msg
    assert "5005" in msg
    assert "operation not permitted" in msg


# --- Real-subprocess integration tests ------------------------------------
#
# These spawn real Bash subprocesses to prove the PG-wide signal actually
# reaps SIGTERM-ignoring children + grandchildren. They use the real
# ``_terminate``, real ``os.killpg``, real ``signal`` delivery. Marked
# ``slow`` because they sleep briefly to let the kernel propagate signals.

_LINUX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="process groups / SIGKILL semantics tested here are POSIX-only",
)


def _spawn_sigterm_ignorer() -> subprocess.Popen[bytes]:
    """Bash subprocess that ignores SIGTERM and sleeps. Returns the Popen.

    Caller spawns with ``start_new_session=True`` to mirror the
    dispatcher's real ``Popen`` call. The shell script traps SIGTERM
    to a no-op then runs ``sleep 300`` — SIGTERM cannot kill it, only
    SIGKILL can.
    """
    script = "trap '' TERM; sleep 300"
    return subprocess.Popen(
        ["bash", "-c", script],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _spawn_sigterm_ignorer_with_child() -> subprocess.Popen[bytes]:
    """Bash subprocess that ignores SIGTERM AND backgrounds a sleeping
    child in the same process group. Returns the parent Popen.

    Used to verify ``os.killpg`` reaps the whole group rather than
    leaving the grandchild as an orphan.
    """
    # Parent: ignore SIGTERM, background a child that also ignores
    # SIGTERM, then wait. The child is in the same PG (no
    # ``setsid``), so a PG-wide SIGKILL must reach it too.
    script = "trap '' TERM; bash -c \"trap '' TERM; sleep 300\" & wait"
    return subprocess.Popen(
        ["bash", "-c", script],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _pid_alive(pid: int) -> bool:
    """True iff ``pid`` is still a live process (os.kill(pid, 0))."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_until_dead(pid: int, *, timeout_s: float = 5.0) -> bool:
    """Poll ``_pid_alive`` for up to ``timeout_s``; return True if dead."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_alive(pid)


@_LINUX_ONLY
@pytest.mark.slow
def test_terminate_kills_real_sigterm_ignoring_subprocess() -> None:
    """Integration: a Bash subprocess that traps SIGTERM and ignores it
    is killed via the SIGKILL escalation. After ``_terminate`` returns,
    the parent pid is reaped by the kernel."""
    process = _spawn_sigterm_ignorer()
    pid = process.pid
    # Let the trap take effect before we signal.
    time.sleep(0.2)
    assert _pid_alive(pid), "fixture failed to start"

    # Use a shorter SIGTERM grace so the test doesn't hold the suite
    # for the full 5s — patch the timeout via a monkey-patched
    # ``process.wait``. The real ``os.killpg`` is exercised; only the
    # grace duration is shortened.
    real_wait = process.wait

    def fast_wait(timeout: float | None = None) -> int:
        # SIGTERM grace: cap at 0.5s so the SIGKILL escalation fires
        # promptly. The post-SIGKILL verification wait uses
        # ``timeout=2`` from ``_terminate`` itself, which is fine.
        return real_wait(timeout=min(timeout or 5.0, 0.5))

    process.wait = fast_wait  # type: ignore[method-assign]

    _terminate(process)  # type: ignore[arg-type]

    assert _wait_until_dead(pid), f"parent pid {pid} still alive after _terminate"
    # Whitespace / pid recycle: the post-condition is "parent gone",
    # which the wait above already established. The integration's job
    # is to prove the SIGKILL escalation actually reached a real
    # SIGTERM-ignorer; the orphan-children case is covered separately.


@_LINUX_ONLY
@pytest.mark.slow
def test_terminate_kills_real_subprocess_pgwide_including_grandchild() -> None:
    """Integration: a Bash subprocess that backgrounds a SIGTERM-ignoring
    child in its own PG is reaped WHOLE — the child dies too because
    ``os.killpg`` targets the group, not just the parent."""
    process = _spawn_sigterm_ignorer_with_child()
    parent_pid = process.pid
    time.sleep(0.3)  # let the bash fork the child

    # Discover the child's pid via /proc — read the parent's children
    # list. On Linux this is exposed via /proc/<pid>/task/<pid>/children.
    children_path = f"/proc/{parent_pid}/task/{parent_pid}/children"
    try:
        with open(children_path) as fh:
            child_pids_raw = fh.read().split()
    except FileNotFoundError:
        pytest.skip(f"{children_path} missing — non-Linux /proc layout")
    child_pids = [int(p) for p in child_pids_raw if p]
    assert child_pids, "fixture failed to fork a child"
    grandchild_pid = child_pids[0]
    assert _pid_alive(grandchild_pid), "fixture child died before we could test it"

    real_wait = process.wait

    def fast_wait(timeout: float | None = None) -> int:
        return real_wait(timeout=min(timeout or 5.0, 0.5))

    process.wait = fast_wait  # type: ignore[method-assign]

    _terminate(process)  # type: ignore[arg-type]

    assert _wait_until_dead(parent_pid), f"parent pid {parent_pid} alive after _terminate"
    assert _wait_until_dead(grandchild_pid), (
        f"grandchild pid {grandchild_pid} alive after _terminate — "
        f"PG-wide kill did not reach forked child"
    )
