"""Tests for cli.supervisor_cmd — stop / drain / status + helpers.

The ``start`` command runs the daemon loop end-to-end (which we do
test in dedicated daemon tests with mocked sources). Here we cover the
read-only / signal-sending surface and the two count helpers that the
status command uses.
"""

from __future__ import annotations

import errno
import json as _json
import os
import re
import signal
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner, Result

from claude_task_runner.cli.supervisor_cmd import (
    _captures_dir,
    _count_in_flight,
    _count_pending,
    app,
)
from claude_task_runner.config.loader import ConfigError, load_settings
from claude_task_runner.queue.schema import Task, TaskState
from claude_task_runner.queue.store import (
    queue_runtime_dir,
    state_path_for,
    task_path_for,
    todo_dir,
    write_state_atomic,
    write_task_atomic,
)
from claude_task_runner.supervisor.persistence import write_atomic as supervisor_write_atomic
from claude_task_runner.supervisor.states import SupervisorSnapshot, SupervisorState


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    return qd


def _make_task(qd: Path, task_id: str) -> Task:
    task = Task.model_validate(
        {
            "id": task_id,
            "title": f"Task {task_id}",
            "prompt": "do the thing",
        }
    )
    write_task_atomic(task, task_path_for(qd, task_id))
    return task


def _seed_state(qd: Path, task_id: str, status: str, **kw: Any) -> TaskState:
    state = TaskState(task_id=task_id, status=status, **kw)
    write_state_atomic(state, state_path_for(qd, task_id))
    return state


_PID = 12345
"""The PID a fake supervisor writes to its PID file."""


class _FakeClock:
    """Stands in for ``time.monotonic`` and ``time.sleep``.

    Time passes only when the code under test sleeps, so a wait of hours
    runs at once and every poll is recorded.
    """

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@contextmanager
def _live_supervisor(
    queue_dir: Path, clock: _FakeClock, *, exits_after: float | None
) -> Iterator[tuple[MagicMock, MagicMock]]:
    """A supervisor that writes :data:`_PID` and exits ``exits_after`` seconds
    into the command, or never with ``None``. Yields the ``os.kill`` and
    ``is_pid_alive`` mocks."""
    start = clock.now
    pid_path = queue_dir / ".claude_task_runner" / "supervisor.pid"
    pid_path.write_text(f"{_PID}\n", encoding="utf-8")

    def alive(pid: int) -> bool:
        assert pid == _PID
        return exits_after is None or clock.now - start < exits_after

    with (
        patch(
            "claude_task_runner.cli.supervisor_cmd.pidfile_mod.is_pid_alive", side_effect=alive
        ) as alive_mock,
        patch("claude_task_runner.process_signals.os.kill") as kill,
        patch("time.monotonic", clock.monotonic),
        patch("time.sleep", clock.sleep),
    ):
        yield kill, alive_mock


def _unreachable(*_args: object, **_kwargs: object) -> object:
    raise AssertionError("reached a step this command must stop before")


@contextmanager
def _signals_nothing() -> Iterator[MagicMock]:
    """For a command that must stop before it looks for a live supervisor.

    ``os.kill`` is a mock, and ``is_pid_alive`` and ``time.sleep`` raise,
    so a regression fails at once. Patching ``os.kill`` alone is not
    enough: ``is_pid_alive`` calls it too, so the mock made any PID look
    alive, and a drain that got that far waited an hour for real.
    Yields the ``os.kill`` mock.
    """
    with (
        patch("claude_task_runner.process_signals.os.kill") as kill,
        patch("claude_task_runner.cli.supervisor_cmd.pidfile_mod.is_pid_alive", _unreachable),
        patch("time.sleep", _unreachable),
    ):
        yield kill


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _invoke(runner: CliRunner, argv: list[str]) -> Result:
    """Invoke the supervisor app wide enough that Rich wraps no line."""
    return runner.invoke(app, argv, env={"COLUMNS": "1000"})


def _plain(text: str) -> str:
    """``text`` without ANSI escapes, in case the environment forces colour."""
    return _ANSI.sub("", text)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def test_captures_dir_path(queue_dir: Path) -> None:
    """`_captures_dir` returns the standard path under the runtime dir."""
    expected = queue_dir / ".claude_task_runner" / "usage_captures"
    assert _captures_dir(queue_dir) == expected


def test_count_pending_zero_when_empty(queue_dir: Path) -> None:
    assert _count_pending(queue_dir) == 0


def test_count_pending_counts_todo_yamls(queue_dir: Path) -> None:
    _make_task(queue_dir, "t1")
    _make_task(queue_dir, "t2")
    _make_task(queue_dir, "t3")
    assert _count_pending(queue_dir) == 3


def test_count_in_flight_counts_running_and_awaiting_sidecar(queue_dir: Path) -> None:
    _make_task(queue_dir, "running1")
    _seed_state(queue_dir, "running1", "running")
    _make_task(queue_dir, "awaiting1")
    _seed_state(queue_dir, "awaiting1", "awaiting_sidecar")
    _make_task(queue_dir, "hung1")
    _seed_state(queue_dir, "hung1", "possibly_hung")
    _make_task(queue_dir, "done1")
    _seed_state(queue_dir, "done1", "completed")
    _make_task(queue_dir, "failed1")
    _seed_state(queue_dir, "failed1", "failed")
    # Exactly the three "in-flight-like" statuses count.
    assert _count_in_flight(queue_dir) == 3


def test_count_in_flight_skips_unparseable_state(queue_dir: Path, monkeypatch) -> None:
    """A state file that won't parse is silently skipped — the doctor
    surfaces it separately. Counter must not crash."""
    _make_task(queue_dir, "t1")
    sp = state_path_for(queue_dir, "t1")
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text("not yaml: ][ broken\n", encoding="utf-8")
    # Even with a broken state, the counter returns 0 and doesn't raise.
    assert _count_in_flight(queue_dir) == 0


def test_count_in_flight_warns_on_unparseable_state(
    queue_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Audit finding 3: skipping an unparseable state file must leave a
    trace — a WARNING that names the offending path — rather than being
    swallowed entirely."""
    _make_task(queue_dir, "t1")
    sp = state_path_for(queue_dir, "t1")
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text("not yaml: ][ broken\n", encoding="utf-8")
    with caplog.at_level("WARNING", logger="claude_task_runner.cli.supervisor_cmd"):
        assert _count_in_flight(queue_dir) == 0
    assert any(
        record.levelname == "WARNING" and str(sp) in record.getMessage()
        for record in caplog.records
    ), caplog.text


# ---------------------------------------------------------------------------
# `stop` command
# ---------------------------------------------------------------------------


def test_stop_no_pid_file(runner: CliRunner, queue_dir: Path) -> None:
    result = runner.invoke(app, ["stop", "--queue", str(queue_dir)])
    assert result.exit_code == 1
    assert "No PID file" in result.stdout


def test_stop_stale_pid(runner: CliRunner, queue_dir: Path) -> None:
    """PID file present but the process is dead → exit 1 with a clear msg."""
    pid_path = queue_dir / ".claude_task_runner" / "supervisor.pid"
    pid_path.write_text("99999\n", encoding="utf-8")  # almost certainly not alive
    with patch(
        "claude_task_runner.cli.supervisor_cmd.pidfile_mod.is_pid_alive", return_value=False
    ):
        result = runner.invoke(app, ["stop", "--queue", str(queue_dir)])
    assert result.exit_code == 1
    assert "not alive" in result.stdout


def test_stop_happy_path_sends_sigterm(runner: CliRunner, queue_dir: Path) -> None:
    pid_path = queue_dir / ".claude_task_runner" / "supervisor.pid"
    pid_path.write_text("12345\n", encoding="utf-8")
    with (
        patch("claude_task_runner.cli.supervisor_cmd.pidfile_mod.is_pid_alive", return_value=True),
        patch("claude_task_runner.process_signals.os.kill") as mock_kill,
    ):
        result = runner.invoke(app, ["stop", "--queue", str(queue_dir)])
    assert result.exit_code == 0
    mock_kill.assert_called_once_with(12345, signal.SIGTERM)
    assert "SIGTERM sent" in result.stdout


def test_stop_process_disappeared(runner: CliRunner, queue_dir: Path) -> None:
    """ProcessLookupError between the is_pid_alive check and the
    os.kill call is a transient race; exit 1 with a clear message."""
    pid_path = queue_dir / ".claude_task_runner" / "supervisor.pid"
    pid_path.write_text("12345\n", encoding="utf-8")
    with (
        patch("claude_task_runner.cli.supervisor_cmd.pidfile_mod.is_pid_alive", return_value=True),
        patch(
            "claude_task_runner.process_signals.os.kill",
            side_effect=ProcessLookupError(),
        ),
    ):
        result = runner.invoke(app, ["stop", "--queue", str(queue_dir)])
    assert result.exit_code == 1
    assert "disappeared" in result.stdout


def test_stop_permission_error(runner: CliRunner, queue_dir: Path) -> None:
    """If the operator can't signal the target PID (different user),
    exit 2 (not 1 — 2 indicates an environmental problem)."""
    pid_path = queue_dir / ".claude_task_runner" / "supervisor.pid"
    pid_path.write_text("424242\n", encoding="utf-8")  # another user's process
    with (
        patch("claude_task_runner.cli.supervisor_cmd.pidfile_mod.is_pid_alive", return_value=True),
        patch(
            "claude_task_runner.process_signals.os.kill",
            side_effect=PermissionError("operation not permitted"),
        ),
    ):
        result = runner.invoke(app, ["stop", "--queue", str(queue_dir)])
    assert result.exit_code == 2
    assert "not allowed" in result.stdout


def test_stop_signals_once_and_does_not_wait(runner: CliRunner, queue_dir: Path) -> None:
    """The systemd unit's fast-stop ``ExecStop`` (ADR-0025) relies on this:
    one SIGTERM, then return, even while the supervisor is still running."""
    clock = _FakeClock()
    with _live_supervisor(queue_dir, clock, exits_after=None) as (kill, alive):
        result = _invoke(runner, ["stop", "--queue", str(queue_dir)])
    assert result.exit_code == 0, result.output
    assert _plain(result.stdout) == f"SIGTERM sent to PID {_PID}.\n"
    kill.assert_called_once_with(_PID, signal.SIGTERM)
    assert alive.call_count == 1
    assert clock.sleeps == []


def test_stop_has_no_timeout_option(runner: CliRunner, queue_dir: Path) -> None:
    """``--timeout`` was accepted from the first release and never read.
    It is gone rather than implemented, since stop must not wait."""
    clock = _FakeClock()
    with _live_supervisor(queue_dir, clock, exits_after=None) as (kill, _):
        result = _invoke(runner, ["stop", "--queue", str(queue_dir), "--timeout", "5"])
    assert result.exit_code == 2
    assert "No such option: --timeout" in _plain(result.output)
    kill.assert_not_called()


@pytest.mark.parametrize("command", ["stop", "drain"])
@pytest.mark.parametrize("kind", ["missing", "a-file"])
def test_stop_and_drain_refuse_a_queue_that_is_not_a_directory(
    runner: CliRunner, tmp_path: Path, command: str, kind: str
) -> None:
    """A mistyped ``--queue`` used to print "No PID file at ...", which
    reads as "the supervisor is not running"."""
    queue = tmp_path / "no-such-queue"
    if kind == "a-file":
        queue.write_text("", encoding="utf-8")
    with _signals_nothing() as kill:
        result = _invoke(runner, [command, "--queue", str(queue)])
    assert result.exit_code == 2
    assert result.stdout == f"--queue is not an existing directory: {queue.resolve()}\n"
    kill.assert_not_called()
    if kind == "missing":
        assert not queue.exists()
    else:
        assert queue.read_text(encoding="utf-8") == ""


_NOT_SIGNALLED = (
    ", so nothing was signalled. A supervisor may still be running: "
    "`pgrep -af 'supervisor start'` lists them.\n"
)


@pytest.mark.parametrize("command", ["stop", "drain"])
@pytest.mark.parametrize(
    ("content", "detail"),
    [
        pytest.param("", "is empty", id="empty"),
        pytest.param("\n", "is empty", id="newline"),
        pytest.param("not-a-pid\n", "holds 'not-a-pid', not a PID", id="words"),
        # is_pid_alive called these dead, so nothing was signalled, but
        # os.kill would send 0 to the caller's process group and -1 to
        # every process the user may signal.
        pytest.param("0\n", "holds '0', not a PID", id="zero"),
        pytest.param("-1\n", "holds '-1', not a PID", id="negative"),
        # os.kill raised OverflowError, so both commands ended in a traceback.
        pytest.param(f"{2**31}\n", "holds '2147483648', not a PID", id="beyond-pid_t"),
    ],
)
def test_a_pid_file_without_a_pid_is_reported(
    runner: CliRunner, queue_dir: Path, command: str, content: str, detail: str
) -> None:
    """It used to read as "No PID file", or as a stale PID, but a
    supervisor may be running with a PID file it has not finished writing."""
    pid_path = queue_dir.resolve() / ".claude_task_runner" / "supervisor.pid"
    pid_path.write_text(content, encoding="utf-8")
    with _signals_nothing() as kill:
        result = _invoke(runner, [command, "--queue", str(queue_dir)])
    assert result.exit_code == 1, result.output
    assert _plain(result.stdout) == f"PID file {pid_path} {detail}{_NOT_SIGNALLED}"
    kill.assert_not_called()


@pytest.mark.parametrize("command", ["stop", "drain"])
def test_an_unreadable_pid_file_is_reported(
    runner: CliRunner, queue_dir: Path, command: str
) -> None:
    pid_path = queue_dir.resolve() / ".claude_task_runner" / "supervisor.pid"
    pid_path.mkdir()
    with _signals_nothing() as kill:
        result = _invoke(runner, [command, "--queue", str(queue_dir)])
    assert result.exit_code == 1, result.output
    assert _plain(result.stdout) == (
        f"cannot read PID file {pid_path}: [Errno {errno.EISDIR}] Is a directory: "
        f"'{pid_path}'{_NOT_SIGNALLED}"
    )
    kill.assert_not_called()


@pytest.mark.parametrize("command", ["stop", "drain"])
@pytest.mark.parametrize("name", ["q[abc]", "q[/]x", "q:b:x"])
def test_the_queue_path_is_printed_as_typed(
    runner: CliRunner, tmp_path: Path, command: str, name: str
) -> None:
    """Printed as Rich markup, ``[abc]`` was dropped from the path and
    ``[/]`` raised ``MarkupError``. Without ``emoji=False``, ``:b:``
    printed as an emoji even with markup off."""
    queue = tmp_path / name
    queue.mkdir(parents=True)  # q[/]x is q[ holding ]x
    with _signals_nothing():
        result = _invoke(runner, [command, "--queue", str(queue)])
    assert result.exit_code == 1, result.output
    pid_path = queue.resolve() / ".claude_task_runner" / "supervisor.pid"
    assert _plain(result.stdout) == f"No PID file at {pid_path}\n"


# ---------------------------------------------------------------------------
# `status` command
# ---------------------------------------------------------------------------


def _make_snapshot(state: SupervisorState, **kw: Any) -> SupervisorSnapshot:
    base: dict[str, Any] = {
        "state": state,
        "since": datetime(2026, 5, 16, 12, 0, 0, tzinfo=UTC),
        "last_5h_util_pct": 18,
        "last_weekly_util_pct": 42,
    }
    base.update(kw)
    return SupervisorSnapshot.model_validate(base)


def test_status_no_snapshot_no_pidfile(runner: CliRunner, queue_dir: Path) -> None:
    """Fresh queue dir: status shows no PID and no snapshot."""
    result = runner.invoke(app, ["status", "--queue", str(queue_dir)])
    assert result.exit_code == 0
    assert "No supervisor.json" in result.stdout
    assert "not running" in result.stdout


def test_status_with_snapshot_human_readable(runner: CliRunner, queue_dir: Path) -> None:
    """Snapshot present: prints state, utilisation, pending and in-flight."""
    snap = _make_snapshot(SupervisorState.DISPATCHING)
    state_path = queue_dir / ".claude_task_runner" / "supervisor.json"
    supervisor_write_atomic(snap, state_path)

    # One pending task in todo/, one running state file.
    _make_task(queue_dir, "pending1")
    _make_task(queue_dir, "running1")
    _seed_state(queue_dir, "running1", "running")

    result = runner.invoke(app, ["status", "--queue", str(queue_dir)])
    assert result.exit_code == 0
    assert "dispatching" in result.stdout
    assert "18%" in result.stdout
    assert "42%" in result.stdout
    assert "Pending:" in result.stdout
    assert "In-flight:" in result.stdout


def test_status_json_output(runner: CliRunner, queue_dir: Path) -> None:
    snap = _make_snapshot(SupervisorState.IDLE)
    state_path = queue_dir / ".claude_task_runner" / "supervisor.json"
    supervisor_write_atomic(snap, state_path)
    result = runner.invoke(app, ["status", "--queue", str(queue_dir), "--json"])
    assert result.exit_code == 0
    payload = _json.loads(result.stdout)
    assert payload["queue_dir"] == str(queue_dir.resolve())
    assert payload["supervisor_alive"] is False
    assert payload["pending"] == 0
    assert payload["in_flight"] == 0
    assert payload["snapshot"]["state"] == "idle"


def test_status_color_categories_render(runner: CliRunner, queue_dir: Path) -> None:
    """Visit all three color branches for state coloring: green / yellow / red.

    The Rich console renders to plain text in tests; we just need the
    state name itself to appear so the formatting code path runs.

    ADR-0022 dropped ``PAUSED_WEEKLY`` / ``END_OF_WEEK_PUSH``; only the
    surviving states are exercised below. ``IDLE`` / ``DISPATCHING`` are
    green; ``SLOWING_DOWN`` is yellow; the rest are red."""
    for state in [
        SupervisorState.IDLE,  # green
        SupervisorState.DISPATCHING,  # green
        SupervisorState.SLOWING_DOWN,  # yellow
        SupervisorState.THROTTLED_5H,  # red
        SupervisorState.THROTTLED_WEEKLY,  # red
        SupervisorState.ERROR_DRIFT,  # red
    ]:
        snap = _make_snapshot(state)
        state_path = queue_dir / ".claude_task_runner" / "supervisor.json"
        supervisor_write_atomic(snap, state_path)
        result = runner.invoke(app, ["status", "--queue", str(queue_dir)])
        assert result.exit_code == 0
        assert state.value in result.stdout


def test_status_with_drift_message(runner: CliRunner, queue_dir: Path) -> None:
    snap = _make_snapshot(
        SupervisorState.ERROR_DRIFT,
        last_drift_message="parser regex did not match",
    )
    state_path = queue_dir / ".claude_task_runner" / "supervisor.json"
    supervisor_write_atomic(snap, state_path)
    result = runner.invoke(app, ["status", "--queue", str(queue_dir)])
    assert result.exit_code == 0
    assert "parser regex did not match" in result.stdout


def test_status_with_scheduled_wakeup(runner: CliRunner, queue_dir: Path) -> None:
    snap = _make_snapshot(
        SupervisorState.THROTTLED_5H,
        scheduled_wakeup_at=datetime(2026, 5, 17, 8, 0, 0, tzinfo=UTC),
    )
    state_path = queue_dir / ".claude_task_runner" / "supervisor.json"
    supervisor_write_atomic(snap, state_path)
    result = runner.invoke(app, ["status", "--queue", str(queue_dir)])
    assert result.exit_code == 0
    assert "Next wakeup" in result.stdout
    assert "2026-05-17" in result.stdout


def test_status_with_alive_pid(runner: CliRunner, queue_dir: Path) -> None:
    """PID file points at a live process — status prints 'alive'."""
    snap = _make_snapshot(SupervisorState.DISPATCHING)
    state_path = queue_dir / ".claude_task_runner" / "supervisor.json"
    supervisor_write_atomic(snap, state_path)
    pid_path = queue_dir / ".claude_task_runner" / "supervisor.pid"
    pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")  # our own PID, definitely alive
    result = runner.invoke(app, ["status", "--queue", str(queue_dir)])
    assert result.exit_code == 0
    assert "alive" in result.stdout


def test_drain_accepts_config_flag(runner: CliRunner, queue_dir: Path, tmp_path: Path) -> None:
    """Regression: ``supervisor drain --config <toml>`` must NOT error
    with ``No such option: --config``.

    Bug history: ``cron/systemd_unit.py::_drain_command_from`` generates
    the ExecStop line by substituting ``supervisor start`` → ``supervisor
    drain`` on the ExecStart command. Since ExecStart includes
    ``--config /path/to/claude_runner.toml``, the resulting ExecStop also
    includes ``--config``. The ``drain`` command did not declare a
    ``--config`` option, so every ``systemctl restart`` saw

        No such option: --config
        Try 'claude-task-runner supervisor drain --help' for help.

    in the journal and ExecStop exited with status=2/INVALIDARGUMENT.
    systemd then fell through to its main SIGTERM kill which still
    triggered the supervisor's graceful-stop path, so end-to-end
    behaviour was correct — but the spurious failure made every restart
    look broken in logs.

    The fix accepts ``--config`` as a no-op on ``drain`` (drain only
    signals the running supervisor via the queue's pidfile; it doesn't
    need settings). This pins the contract so the systemd-unit
    generator and the drain CLI stay in sync.
    """
    config_path = tmp_path / "claude_runner.toml"
    config_path.write_text("", encoding="utf-8")
    # No pidfile → drain exits 1 with "No PID file" (the same path
    # test_stop_no_pid_file exercises). The point of this test is that
    # we reach that exit-1 instead of typer's "No such option" exit-2.
    result = runner.invoke(
        app,
        ["drain", "--config", str(config_path), "--queue", str(queue_dir)],
    )
    assert result.exit_code == 1, (
        f"expected exit 1 (no PID file); got {result.exit_code}.\noutput: {result.output!r}"
    )
    assert "No such option" not in result.output
    assert "No PID file" in result.stdout


def test_drain_config_short_flag_also_accepted(
    runner: CliRunner, queue_dir: Path, tmp_path: Path
) -> None:
    """``-c`` short flag also works (matches other commands' pattern)."""
    config_path = tmp_path / "claude_runner.toml"
    config_path.write_text("", encoding="utf-8")
    result = runner.invoke(
        app,
        ["drain", "-c", str(config_path), "--queue", str(queue_dir)],
    )
    assert result.exit_code == 1, (
        f"expected exit 1 (no PID file); got {result.exit_code}.\noutput: {result.output!r}"
    )
    assert "No such option" not in result.output


def test_drain_systemd_unit_execstop_argv_is_accepted_by_drain_cli(
    runner: CliRunner, queue_dir: Path, tmp_path: Path
) -> None:
    """Lock the contract between the systemd unit generator and drain.

    The generator (``cron/systemd_unit.py::_drain_command_from``) takes
    the ExecStart command and substitutes ``start`` → ``drain``, then
    appends ``--no-wait``. Every flag on ExecStart that isn't stripped
    by the generator MUST be accepted by drain. This test exercises the
    full ExecStop argv the generator would produce.
    """
    from claude_task_runner.cron.systemd_unit import _drain_command_from

    config_path = tmp_path / "claude_runner.toml"
    config_path.write_text("", encoding="utf-8")
    supervisor_command = (
        f"/usr/local/bin/claude-task-runner supervisor start "
        f"--queue {queue_dir} --config {config_path}"
    )
    drain_command = _drain_command_from(supervisor_command)
    # Drop the binary path; CliRunner invokes the typer app directly.
    drain_argv = drain_command.split(" ", 1)[1].split()
    # Strip "supervisor" since CliRunner is rooted at the supervisor sub-app
    # (see the fixture-level import: `from claude_task_runner.cli.supervisor_cmd import app`).
    assert drain_argv[0] == "supervisor"
    drain_argv = drain_argv[1:]  # ["drain", "--queue", ..., "--config", ..., "--no-wait"]
    result = runner.invoke(app, drain_argv)
    assert result.exit_code == 1, (
        f"systemd-generated ExecStop argv was rejected by drain.\n"
        f"argv: {drain_argv}\n"
        f"exit: {result.exit_code}\n"
        f"output: {result.output!r}"
    )
    assert "No such option" not in result.output


def test_drain_no_wait_sends_sigusr1_and_returns(runner: CliRunner, queue_dir: Path) -> None:
    clock = _FakeClock()
    with _live_supervisor(queue_dir, clock, exits_after=None) as (kill, alive):
        result = _invoke(runner, ["drain", "--queue", str(queue_dir), "--no-wait"])
    assert result.exit_code == 0, result.output
    assert _plain(result.stdout) == f"SIGUSR1 (drain) sent to PID {_PID}.\n"
    kill.assert_called_once_with(_PID, signal.SIGUSR1)
    assert alive.call_count == 1
    assert clock.sleeps == []


def test_drain_waits_until_the_supervisor_exits(runner: CliRunner, queue_dir: Path) -> None:
    clock = _FakeClock()
    with _live_supervisor(queue_dir, clock, exits_after=5.0) as (kill, _):
        result = _invoke(runner, ["drain", "--queue", str(queue_dir)])
    assert result.exit_code == 0, result.output
    assert _plain(result.stdout) == (
        f"SIGUSR1 (drain) sent to PID {_PID}.\n"
        f"Waiting up to 14640s for PID {_PID} to exit (polling every 2s)...\n"
        f"PID {_PID} exited; drain complete.\n"
    )
    kill.assert_called_once_with(_PID, signal.SIGUSR1)
    assert clock.sleeps == [2.0, 2.0, 2.0]


_STILL_DRAINING = (
    "The supervisor keeps draining; re-run `supervisor drain` to wait again. "
    "`supervisor stop` would not end the tasks it is waiting for: with "
    "[supervisor].adopt_workers on, the next supervisor adopts them, and with it "
    "off, the supervisor waits for them before it exits.\n"
)
"""The end of drain's message when its wait runs out. It used to say that
``supervisor stop`` force-exits and that "in-flight tasks will be killed by
systemd's KillMode", but the unit's ``KillMode=process`` never signals the
workers, and with adoption off a stopped supervisor still waits for them."""


def test_drain_waits_for_the_task_cap_by_default(runner: CliRunner, queue_dir: Path) -> None:
    """It waited 3600 s whatever the cap, so with the package defaults a
    drain whose last task ran to its 4 h cap gave up with exit 4 while
    the supervisor was still draining. It now waits for the cap, both
    hook timeouts and one supervisor tick."""
    defaults = load_settings(None)
    assert (
        defaults.task_caps.max_duration_s_per_task,
        defaults.hooks.pre_dispatch_timeout_s,
        defaults.hooks.post_dispatch_timeout_s,
        defaults.usage.poll_interval_s,
    ) == (14400, 120, 60, 60)
    clock = _FakeClock()
    with _live_supervisor(queue_dir, clock, exits_after=None) as (kill, _):
        result = _invoke(runner, ["drain", "--queue", str(queue_dir)])
    assert result.exit_code == 4, result.output
    assert clock.sleeps == [2.0] * 7320
    assert _plain(result.stdout) == (
        f"SIGUSR1 (drain) sent to PID {_PID}.\n"
        f"Waiting up to 14640s for PID {_PID} to exit (polling every 2s)...\n"
        f"Drain still in progress after 14640s. {_STILL_DRAINING}"
    )
    kill.assert_called_once_with(_PID, signal.SIGUSR1)


_QUEUE_SETTINGS = """\
[task_caps]
max_duration_s_per_task = 100

[hooks]
pre_dispatch_timeout_s = 10
post_dispatch_timeout_s = 5

[usage]
poll_interval_s = 7
"""
"""Settings whose default drain wait is 10 + 100 + 5 + 7 = 122 s."""


@pytest.mark.parametrize("where", ["queue", "--config"])
def test_drain_default_wait_follows_the_queue_settings(
    runner: CliRunner, queue_dir: Path, tmp_path: Path, where: str
) -> None:
    """From ``<queue>/claude_runner.toml``, or from ``--config``, which wins."""
    if where == "queue":
        (queue_dir / "claude_runner.toml").write_text(_QUEUE_SETTINGS, encoding="utf-8")
        extra: list[str] = []
    else:
        # A queue TOML with no cap, which --config overrides.
        zero_cap = "[task_caps]\nmax_duration_s_per_task = 0\n"
        (queue_dir / "claude_runner.toml").write_text(zero_cap, encoding="utf-8")
        config = tmp_path / "elsewhere.toml"
        config.write_text(_QUEUE_SETTINGS, encoding="utf-8")
        extra = ["--config", str(config)]
    clock = _FakeClock()
    with _live_supervisor(queue_dir, clock, exits_after=None):
        result = _invoke(runner, ["drain", "--queue", str(queue_dir), *extra])
    assert result.exit_code == 4, result.output
    assert clock.sleeps == [2.0] * 61
    assert _plain(result.stdout) == (
        f"SIGUSR1 (drain) sent to PID {_PID}.\n"
        f"Waiting up to 122s for PID {_PID} to exit (polling every 2s)...\n"
        f"Drain still in progress after 122s. {_STILL_DRAINING}"
    )


def test_drain_waits_without_limit_when_the_cap_is_0(runner: CliRunner, queue_dir: Path) -> None:
    (queue_dir / "claude_runner.toml").write_text(
        "[task_caps]\nmax_duration_s_per_task = 0\n", encoding="utf-8"
    )
    clock = _FakeClock()
    with _live_supervisor(queue_dir, clock, exits_after=30000.0):
        result = _invoke(runner, ["drain", "--queue", str(queue_dir)])
    assert result.exit_code == 0, result.output
    assert clock.sleeps == [2.0] * 15000
    assert _plain(result.stdout) == (
        f"SIGUSR1 (drain) sent to PID {_PID}.\n"
        f"Waiting with no time limit for PID {_PID} to exit (polling every 2s)...\n"
        f"PID {_PID} exited; drain complete.\n"
    )


def test_drain_honours_an_explicit_timeout_and_poll(runner: CliRunner, queue_dir: Path) -> None:
    clock = _FakeClock()
    with _live_supervisor(queue_dir, clock, exits_after=None):
        result = _invoke(
            runner, ["drain", "--queue", str(queue_dir), "--timeout", "10", "--poll", "3"]
        )
    assert result.exit_code == 4, result.output
    assert clock.sleeps == [3.0, 3.0, 3.0, 3.0]
    assert _plain(result.stdout) == (
        f"SIGUSR1 (drain) sent to PID {_PID}.\n"
        f"Waiting up to 10s for PID {_PID} to exit (polling every 3s)...\n"
        f"Drain still in progress after 10s. {_STILL_DRAINING}"
    )


def test_drain_prints_a_fractional_poll(runner: CliRunner, queue_dir: Path) -> None:
    """``{:.0f}`` printed ``--poll 0.5`` as "polling every 0s"."""
    clock = _FakeClock()
    with _live_supervisor(queue_dir, clock, exits_after=None):
        result = _invoke(
            runner, ["drain", "--queue", str(queue_dir), "--timeout", "1", "--poll", "0.5"]
        )
    assert result.exit_code == 4, result.output
    assert clock.sleeps == [0.5, 0.5]
    assert f"Waiting up to 1s for PID {_PID} to exit (polling every 0.5s)...\n" in _plain(
        result.stdout
    )


_BROKEN_TOML = "this is [not toml\n"


@pytest.mark.parametrize("where", ["--config", "queue"])
@pytest.mark.parametrize("how", [["--no-wait"], ["--timeout", "10"]])
def test_drain_reads_no_settings_without_the_default_wait(
    runner: CliRunner, queue_dir: Path, tmp_path: Path, where: str, how: list[str]
) -> None:
    """``--no-wait``, which the adopt-off systemd unit's ``ExecStop``
    runs, and an explicit ``--timeout`` never read the settings, so a
    TOML that does not load changes nothing."""
    broken = tmp_path / "broken.toml" if where == "--config" else queue_dir / "claude_runner.toml"
    broken.write_text(_BROKEN_TOML, encoding="utf-8")
    extra = ["--config", str(broken)] if where == "--config" else []
    clock = _FakeClock()
    with _live_supervisor(queue_dir, clock, exits_after=None) as (kill, _):
        result = _invoke(runner, ["drain", "--queue", str(queue_dir), *extra, *how])
    assert result.exit_code == (0 if how == ["--no-wait"] else 4), result.output
    assert "cannot load" not in result.output
    kill.assert_called_once_with(_PID, signal.SIGUSR1)


def _settings_error(path: Path) -> str:
    """What ``load_settings`` raises for ``path``."""
    with pytest.raises((ConfigError, OSError)) as exc_info:
        load_settings(path)
    return str(exc_info.value)


@pytest.mark.parametrize("where", ["--config", "queue", "missing", "directory"])
def test_waiting_drain_refuses_settings_that_do_not_load(
    runner: CliRunner, queue_dir: Path, tmp_path: Path, where: str
) -> None:
    """Refused with exit 2 before anything is signalled, since the
    default wait needs the cap."""
    config = queue_dir / "claude_runner.toml" if where == "queue" else tmp_path / "c.toml"
    if where in ("--config", "queue"):
        config.write_text(_BROKEN_TOML, encoding="utf-8")
    elif where == "directory":
        config.mkdir()
    extra = [] if where == "queue" else ["--config", str(config)]
    clock = _FakeClock()
    with _live_supervisor(queue_dir, clock, exits_after=None) as (kill, _):
        result = _invoke(runner, ["drain", "--queue", str(queue_dir), *extra])
    assert result.exit_code == 2, result.output
    assert _plain(result.stdout) == (
        f"cannot load the settings for the default --timeout: {_settings_error(config)}. "
        "Pass --timeout <seconds>, or --no-wait, to drain without them.\n"
    )
    kill.assert_not_called()
    assert clock.sleeps == []


def test_drain_reports_no_supervisor_before_reading_settings(
    runner: CliRunner, queue_dir: Path
) -> None:
    """With nothing to drain, a TOML that does not load does not matter."""
    (queue_dir / "claude_runner.toml").write_text(_BROKEN_TOML, encoding="utf-8")
    with _signals_nothing():
        result = _invoke(runner, ["drain", "--queue", str(queue_dir)])
    assert result.exit_code == 1, result.output
    pid_path = queue_dir.resolve() / ".claude_task_runner" / "supervisor.pid"
    assert _plain(result.stdout) == f"No PID file at {pid_path}\n"


def test_drain_stale_pid(runner: CliRunner, queue_dir: Path) -> None:
    clock = _FakeClock()
    with _live_supervisor(queue_dir, clock, exits_after=0.0) as (kill, _):
        result = _invoke(runner, ["drain", "--queue", str(queue_dir)])
    assert result.exit_code == 1
    assert _plain(result.stdout) == f"PID {_PID} not alive (stale PID file)\n"
    kill.assert_not_called()


def test_drain_process_disappeared(runner: CliRunner, queue_dir: Path) -> None:
    clock = _FakeClock()
    with _live_supervisor(queue_dir, clock, exits_after=None) as (kill, _):
        kill.side_effect = ProcessLookupError()
        result = _invoke(runner, ["drain", "--queue", str(queue_dir)])
    assert result.exit_code == 1
    assert _plain(result.stdout) == f"PID {_PID} disappeared before SIGUSR1\n"
    assert clock.sleeps == []


def test_drain_permission_error(runner: CliRunner, queue_dir: Path) -> None:
    clock = _FakeClock()
    with _live_supervisor(queue_dir, clock, exits_after=None) as (kill, _):
        kill.side_effect = PermissionError("operation not permitted")
        result = _invoke(runner, ["drain", "--queue", str(queue_dir)])
    assert result.exit_code == 2
    assert _plain(result.stdout) == f"not allowed to signal PID {_PID}: operation not permitted\n"
    assert clock.sleeps == []


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["missing", "a-file"])
def test_start_refuses_a_queue_that_is_not_a_directory(
    runner: CliRunner, tmp_path: Path, kind: str
) -> None:
    """``queue_runtime_dir`` used to create a mistyped or deleted ``--queue``.

    The supervisor started on that empty queue held the per-user global
    lock, so the real queue's supervisor failed with "another supervisor is
    already running"."""
    queue = tmp_path / "no-such-queue"
    if kind == "a-file":
        queue.write_text("", encoding="utf-8")
    with patch("claude_task_runner.cli.supervisor_cmd.start_daemon") as mock_start:
        result = runner.invoke(app, ["start", "--queue", str(queue)])
    assert result.exit_code == 2
    assert result.stdout == f"--queue is not an existing directory: {queue.resolve()}\n"
    mock_start.assert_not_called()
    if kind == "missing":
        assert not queue.exists()
    else:
        assert queue.read_text(encoding="utf-8") == ""


def test_start_runs_the_daemon_on_an_existing_queue(runner: CliRunner, tmp_path: Path) -> None:
    queue = tmp_path / "q"
    queue.mkdir()
    with (
        patch("claude_task_runner.cli.supervisor_cmd.configure_logging"),
        patch("claude_task_runner.cli.supervisor_cmd.start_daemon") as mock_start,
    ):
        result = runner.invoke(app, ["start", "--queue", str(queue)])
    assert result.exit_code == 0, result.output
    mock_start.assert_called_once()
    assert mock_start.call_args.kwargs["queue_dir"] == queue.resolve()
