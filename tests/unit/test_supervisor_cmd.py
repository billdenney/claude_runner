"""Tests for cli.supervisor_cmd — stop / status + helpers.

The ``start`` command runs the daemon loop end-to-end (which we do
test in dedicated daemon tests with mocked sources). Here we cover the
read-only / signal-sending surface and the two count helpers that the
status command uses.
"""

from __future__ import annotations

import json as _json
import os
import signal
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from claude_task_runner.cli.supervisor_cmd import (
    _captures_dir,
    _count_in_flight,
    _count_pending,
    app,
)
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
        patch("claude_task_runner.cli.supervisor_cmd.os.kill") as mock_kill,
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
            "claude_task_runner.cli.supervisor_cmd.os.kill",
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
    pid_path.write_text("1\n", encoding="utf-8")  # init
    with (
        patch("claude_task_runner.cli.supervisor_cmd.pidfile_mod.is_pid_alive", return_value=True),
        patch(
            "claude_task_runner.cli.supervisor_cmd.os.kill",
            side_effect=PermissionError("operation not permitted"),
        ),
    ):
        result = runner.invoke(app, ["stop", "--queue", str(queue_dir)])
    assert result.exit_code == 2
    assert "not allowed" in result.stdout


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
        SupervisorState.STOPPED,  # red
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
