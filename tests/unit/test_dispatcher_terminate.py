"""Tests for the dispatcher's process-group termination path.

``_terminate`` is the cap-kill / silence-kill escalation. After
ADR-0021 (orphan-child fix) it signals the subprocess's whole process
group rather than just the parent pid, so children ``claude`` forked
(MCP servers, shelled-out ``git`` / ``Rscript``) are reaped too. The
subprocess is spawned with ``start_new_session=True`` precisely so the
group exists to be signalled.

These tests exercise ``_terminate`` and ``_signal_group`` directly with
a fake process and patched ``os.killpg`` / ``os.getpgid`` so no real
process or signal is involved:

* SIGTERM is sent to the group; on a clean ``wait()`` no SIGKILL fires.
* SIGTERM → (wait TimeoutExpired) → SIGKILL escalation, both on the
  group (the audit BUG #2 / coverage A path).
* A vanished group (``ProcessLookupError``) is swallowed silently.
* Any other ``OSError`` on signal-send is logged, not swallowed
  (audit BUG #3).
"""

from __future__ import annotations

import logging
import signal
from unittest import mock

import pytest

from claude_task_runner.runner import dispatcher as dispatcher_mod
from claude_task_runner.runner.dispatcher import _signal_group, _terminate


class _FakeProcess:
    """Minimal ``subprocess.Popen`` shape ``_terminate`` touches.

    ``wait`` either returns ``0`` or raises ``TimeoutExpired`` depending
    on ``timeout_raises``. The real Popen object's ``send_signal`` /
    ``kill`` are NOT used by ``_terminate`` anymore (it goes through
    ``os.killpg``), so they're absent on purpose — a regression that
    reintroduces a pid-only ``process.kill()`` would ``AttributeError``
    here and fail loudly.
    """

    def __init__(self, *, pid: int = 4242, timeout_raises: bool = False) -> None:
        self.pid = pid
        self._timeout_raises = timeout_raises
        self.wait_calls: list[float | None] = []

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        if self._timeout_raises:
            raise dispatcher_mod.subprocess.TimeoutExpired(cmd="claude", timeout=timeout or 0)
        return 0


def test_terminate_sigterms_group_then_no_kill_on_clean_wait() -> None:
    """A subprocess that exits within the grace period gets exactly one
    SIGTERM to its group and no SIGKILL."""
    process = _FakeProcess(pid=1000, timeout_raises=False)

    with (
        mock.patch.object(dispatcher_mod.os, "getpgid", return_value=1000) as getpgid,
        mock.patch.object(dispatcher_mod.os, "killpg") as killpg,
    ):
        _terminate(process)  # type: ignore[arg-type]

    getpgid.assert_called_once_with(1000)
    killpg.assert_called_once_with(1000, signal.SIGTERM)
    # The 5s grace wait happened exactly once.
    assert process.wait_calls == [5]


def test_terminate_escalates_sigterm_then_sigkill_on_timeout() -> None:
    """Coverage A / BUG #2: when ``wait()`` times out the group is
    SIGKILLed after the SIGTERM, both via ``os.killpg`` on the pgid."""
    process = _FakeProcess(pid=2000, timeout_raises=True)

    with (
        mock.patch.object(dispatcher_mod.os, "getpgid", return_value=2000),
        mock.patch.object(dispatcher_mod.os, "killpg") as killpg,
    ):
        _terminate(process)  # type: ignore[arg-type]

    # Exactly two group signals, in order: SIGTERM then SIGKILL.
    assert killpg.call_args_list == [
        mock.call(2000, signal.SIGTERM),
        mock.call(2000, signal.SIGKILL),
    ]


def test_signal_group_swallows_process_lookup_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A group that already vanished (``ProcessLookupError``) is benign
    — no log, no raise."""
    process = _FakeProcess(pid=3000)

    with (
        mock.patch.object(dispatcher_mod.os, "getpgid", return_value=3000),
        mock.patch.object(
            dispatcher_mod.os, "killpg", side_effect=ProcessLookupError("no such process")
        ),
        caplog.at_level(logging.DEBUG, logger="claude_task_runner.runner.dispatcher"),
    ):
        _signal_group(process, signal.SIGTERM)  # type: ignore[arg-type]

    # Nothing logged for the benign already-gone case.
    assert caplog.records == []


def test_signal_group_logs_other_oserror(caplog: pytest.LogCaptureFixture) -> None:
    """BUG #3: a non-vanished ``OSError`` (e.g. EPERM) on signal-send is
    logged at ERROR with the pid, never silently swallowed."""
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


def test_terminate_logs_when_sigkill_races_process_exit(caplog: pytest.LogCaptureFixture) -> None:
    """BUG #4: the post-timeout SIGKILL can itself hit a now-gone group.
    ``ProcessLookupError`` is benign (swallowed), but any other OSError
    on the escalation kill is still surfaced via ``_signal_group``'s
    error log rather than crashing ``_terminate``."""
    process = _FakeProcess(pid=6006, timeout_raises=True)

    def killpg_side_effect(_pgid: int, sig: int) -> None:
        # SIGTERM succeeds; the escalation SIGKILL hits EPERM.
        if sig == signal.SIGKILL:
            raise PermissionError("operation not permitted")

    with (
        mock.patch.object(dispatcher_mod.os, "getpgid", return_value=6006),
        mock.patch.object(dispatcher_mod.os, "killpg", side_effect=killpg_side_effect),
        caplog.at_level(logging.ERROR, logger="claude_task_runner.runner.dispatcher"),
    ):
        # Must not raise.
        _terminate(process)  # type: ignore[arg-type]

    error_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("terminate signal failed" in m and "6006" in m for m in error_msgs)


class _FakeProcessVerify:
    """Two-stage Popen drop-in: SIGTERM wait times out; SIGKILL wait
    is configurable.

    The first ``wait(timeout=5)`` raises ``TimeoutExpired`` so the
    escalation runs; the second ``wait(timeout=2)`` either returns 0
    (kill landed) or raises ``TimeoutExpired`` (subprocess still in
    D-state). Lets us exercise the post-SIGKILL verify branch added
    in the zombie-consolidated PR.
    """

    def __init__(self, *, pid: int, second_wait_times_out: bool) -> None:
        self.pid = pid
        self._wait_count = 0
        self._second_wait_times_out = second_wait_times_out
        self.wait_calls: list[float | None] = []

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        self._wait_count += 1
        if self._wait_count == 1:
            # First wait: simulate "subprocess ignored SIGTERM" so the
            # escalation to SIGKILL fires.
            raise dispatcher_mod.subprocess.TimeoutExpired(cmd="claude", timeout=timeout or 0)
        if self._second_wait_times_out:
            # SIGKILL post-verify also timed out (D-state).
            raise dispatcher_mod.subprocess.TimeoutExpired(cmd="claude", timeout=timeout or 0)
        return 0


def test_terminate_post_kill_verifies_with_second_wait() -> None:
    """After SIGKILL ``_terminate`` runs a second ``wait(timeout=2)``.

    Post-kill verification is what surfaces a TASK_UNINTERRUPTIBLE
    (D-state) leak — without the second wait a SIGKILL that didn't
    land silently returns and the caller proceeds to free the slot.
    Exercises the second-wait branch added in the 2026-06
    zombie-consolidated PR."""
    process = _FakeProcessVerify(pid=7007, second_wait_times_out=False)

    with (
        mock.patch.object(dispatcher_mod.os, "getpgid", return_value=7007),
        mock.patch.object(dispatcher_mod.os, "killpg"),
    ):
        _terminate(process)  # type: ignore[arg-type]

    # Two wait() calls in order: 5s grace then 2s post-kill verify.
    assert process.wait_calls == [5, 2]


def test_terminate_logs_when_subprocess_survives_sigkill(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A subprocess still alive 2s after group SIGKILL → ERROR log.

    The error message includes the pid and the ``subprocess leak``
    fingerprint so the orchestrator's tick-level reap can correlate
    it with its own ``SUBPROCESS_LEAK_DETECTED`` log, and so an
    operator grepping the journal for "subprocess leak" finds the
    earliest evidence (dispatcher) and the held-slot consequence
    (orchestrator) on the same scan."""
    process = _FakeProcessVerify(pid=8008, second_wait_times_out=True)

    with (
        mock.patch.object(dispatcher_mod.os, "getpgid", return_value=8008),
        mock.patch.object(dispatcher_mod.os, "killpg"),
        caplog.at_level(logging.ERROR, logger="claude_task_runner.runner.dispatcher"),
    ):
        _terminate(process)  # type: ignore[arg-type]

    error_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("subprocess leak" in m and "8008" in m for m in error_msgs)
