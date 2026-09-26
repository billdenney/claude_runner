"""Tests for cli/install_cmd.py — install / uninstall watchdog.

Mocks ``systemctl``, ``crontab``, and any other subprocess invocation
so no real watchdog is ever wired up. Also mocks ``shutil.which`` so
PATH lookups are deterministic regardless of the developer's machine.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner, Result

from claude_task_runner.cli import watchdog_cmd
from claude_task_runner.cli.install_cmd import (
    _detect_init_system,
    _supervisor_command,
    _watchdog_script_path,
    app,
)
from claude_task_runner.config.loader import ConfigError, load_settings
from claude_task_runner.config.schema import WatchdogSettings
from claude_task_runner.cron import systemd_unit as systemd_mod
from claude_task_runner.cron.registry import (
    load_registered_queues,
    queues_registry_path,
    register_queue,
)
from claude_task_runner.supervisor.pidfile import acquire_global_lock


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``Path.home()`` for every test in this file.

    A cron ``install`` registers its queue in
    ``~/.claude_task_runner/queues.json``, so without this the tests
    would write into the developer's real watchdog registry. The home
    is a subdirectory so it never coincides with a test's queue dir
    (the tests below pass ``tmp_path`` itself as ``--queue``)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def test_watchdog_script_path_resolves() -> None:
    """The packaged watchdog.sh is shipped with the source tree."""
    p = _watchdog_script_path()
    assert p.name == "watchdog.sh"
    # We don't require it to exist (could be a source-only checkout
    # without the script), but the path is well-formed.
    assert p.is_absolute()


def test_supervisor_command_with_queue_only() -> None:
    with patch(
        "claude_task_runner.cli.install_cmd.shutil.which",
        return_value="/usr/local/bin/claude-task-runner",
    ):
        cmd = _supervisor_command(Path("/home/u/queue"))
    assert cmd == ("/usr/local/bin/claude-task-runner supervisor start --queue /home/u/queue")


def test_supervisor_command_with_config() -> None:
    with patch(
        "claude_task_runner.cli.install_cmd.shutil.which",
        return_value="/usr/local/bin/claude-task-runner",
    ):
        cmd = _supervisor_command(
            Path("/home/u/queue"),
            config=Path("/home/u/queue/claude_runner.toml"),
        )
    assert "--config /home/u/queue/claude_runner.toml" in cmd


def test_supervisor_command_raises_when_not_on_path() -> None:
    """Missing binary → typer.Exit code 2."""
    import typer

    with patch("claude_task_runner.cli.install_cmd.shutil.which", return_value=None):
        with pytest.raises(typer.Exit) as exc_info:
            _supervisor_command(Path("/queue"))
        assert exc_info.value.exit_code == 2


def test_detect_init_system_systemd_explicit() -> None:
    assert _detect_init_system("systemd") == "systemd"


def test_detect_init_system_cron_explicit() -> None:
    assert _detect_init_system("cron") == "cron"


def test_detect_init_system_auto_prefers_systemd() -> None:
    with patch(
        "claude_task_runner.cli.install_cmd.systemd_mod.is_systemd_user_available",
        return_value=True,
    ):
        assert _detect_init_system("auto") == "systemd"


def test_detect_init_system_auto_falls_back_to_cron() -> None:
    with patch(
        "claude_task_runner.cli.install_cmd.systemd_mod.is_systemd_user_available",
        return_value=False,
    ):
        assert _detect_init_system("auto") == "cron"


# ---------------------------------------------------------------------------
# `install` — systemd branch
# ---------------------------------------------------------------------------


def _systemd_plan_mock(*, block_existed: bool = False, unit_path: Path | None = None) -> Any:
    """Build a mock InstallPlan-like object that build_install_plan can return."""
    return MagicMock(
        block_existed=block_existed,
        unit_path=unit_path or Path("/tmp/test.service"),
        unit_text="[Unit]\nDescription=test\n",
        enable_command=["systemctl", "--user", "enable", "--now", "claude-task-runner.service"],
        existing_text=None,
    )


def test_install_systemd_happy_path(runner: CliRunner, tmp_path: Path) -> None:
    """--yes installs without prompting; systemd path."""
    with (
        patch(
            "claude_task_runner.cli.install_cmd._detect_init_system",
            return_value="systemd",
        ),
        patch(
            "claude_task_runner.cli.install_cmd.systemd_mod.build_install_plan",
            return_value=_systemd_plan_mock(unit_path=tmp_path / "ctr.service"),
        ),
        patch(
            "claude_task_runner.cli.install_cmd.systemd_mod.apply_plan",
        ) as mock_apply,
        patch(
            "claude_task_runner.cli.install_cmd.shutil.which",
            return_value="/usr/local/bin/claude-task-runner",
        ),
    ):
        result = runner.invoke(app, ["--yes", "--queue", str(tmp_path)])
    assert result.exit_code == 0
    mock_apply.assert_called_once()
    assert "systemd unit installed" in result.stdout


def test_install_systemd_replace_when_block_existed(runner: CliRunner, tmp_path: Path) -> None:
    with (
        patch(
            "claude_task_runner.cli.install_cmd._detect_init_system",
            return_value="systemd",
        ),
        patch(
            "claude_task_runner.cli.install_cmd.systemd_mod.build_install_plan",
            return_value=_systemd_plan_mock(block_existed=True),
        ),
        patch("claude_task_runner.cli.install_cmd.systemd_mod.apply_plan"),
        patch(
            "claude_task_runner.cli.install_cmd.shutil.which",
            return_value="/usr/local/bin/claude-task-runner",
        ),
    ):
        result = runner.invoke(app, ["--yes", "--queue", str(tmp_path)])
    assert result.exit_code == 0
    assert "replace" in result.stdout


def test_install_systemd_aborts_on_no(runner: CliRunner, tmp_path: Path) -> None:
    with (
        patch(
            "claude_task_runner.cli.install_cmd._detect_init_system",
            return_value="systemd",
        ),
        patch(
            "claude_task_runner.cli.install_cmd.systemd_mod.build_install_plan",
            return_value=_systemd_plan_mock(),
        ),
        patch("claude_task_runner.cli.install_cmd.systemd_mod.apply_plan") as mock_apply,
        patch(
            "claude_task_runner.cli.install_cmd.shutil.which",
            return_value="/usr/local/bin/claude-task-runner",
        ),
    ):
        result = runner.invoke(app, ["--queue", str(tmp_path)], input="n\n")
    assert result.exit_code == 1
    mock_apply.assert_not_called()
    assert "Aborted" in result.stdout


def test_install_systemd_apply_failure(runner: CliRunner, tmp_path: Path) -> None:
    from claude_task_runner.cron.systemd_unit import SystemdError

    with (
        patch(
            "claude_task_runner.cli.install_cmd._detect_init_system",
            return_value="systemd",
        ),
        patch(
            "claude_task_runner.cli.install_cmd.systemd_mod.build_install_plan",
            return_value=_systemd_plan_mock(),
        ),
        patch(
            "claude_task_runner.cli.install_cmd.systemd_mod.apply_plan",
            side_effect=SystemdError("daemon-reload failed"),
        ),
        patch(
            "claude_task_runner.cli.install_cmd.shutil.which",
            return_value="/usr/local/bin/claude-task-runner",
        ),
    ):
        result = runner.invoke(app, ["--yes", "--queue", str(tmp_path)])
    assert result.exit_code == 2
    assert "systemd install failed" in result.stdout


# ---------------------------------------------------------------------------
# `install` — systemd branch, with the unit already running
# ---------------------------------------------------------------------------

_EXE = "/usr/local/bin/claude-task-runner"


def _write_installed_unit(
    queue: Path,
    *,
    exe: str = _EXE,
    config: Path | None = None,
    watchdog: WatchdogSettings | None = None,
    adopt_workers: bool = True,
) -> None:
    """Write the unit an earlier ``install --queue <queue>`` would have written.

    It has the package's ``[task_caps]``, so with adoption off it waits
    14400 s for a drain."""
    command = f"{exe} supervisor start --queue {queue}"
    if config is not None:
        command += f" --config {config}"
    path = systemd_mod.systemd_unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        systemd_mod.build_unit_text(
            supervisor_command=command,
            queue_dir=queue,
            watchdog=watchdog if watchdog is not None else load_settings(None).watchdog,
            task_caps=load_settings(None).task_caps,
            adopt_workers=adopt_workers,
        ),
        encoding="utf-8",
    )


@contextmanager
def _systemd_install(*, active: bool | None) -> Iterator[MagicMock]:
    """Patch out the systemd branch's I/O; yield the ``apply_plan`` mock.

    ``active`` is what ``is_unit_active`` answers; with ``None`` any
    question to systemd fails the test."""

    def _is_active(*_args: object, **_kwargs: object) -> bool:
        if active is None:
            raise AssertionError("asked systemd whether the unit is active")
        return active

    with (
        patch(
            "claude_task_runner.cli.install_cmd._detect_init_system",
            return_value="systemd",
        ),
        patch("claude_task_runner.cli.install_cmd.shutil.which", return_value=_EXE),
        patch(
            "claude_task_runner.cli.install_cmd.systemd_mod.is_unit_active",
            side_effect=_is_active,
        ),
        patch("claude_task_runner.cli.install_cmd.systemd_mod.apply_plan") as mock_apply,
    ):
        yield mock_apply


def test_install_systemd_says_the_running_unit_keeps_the_old_queue(
    runner: CliRunner, tmp_path: Path
) -> None:
    """``enable --now`` does not restart an active unit; systemd 255 keeps the old process.

    install used to print "systemd unit installed and started." while the
    old queue's supervisor went on running and holding the per-user lock."""
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    _write_installed_unit(old.resolve())
    with _systemd_install(active=True) as mock_apply:
        result = runner.invoke(app, ["--queue", str(new)], input="y\n")
    assert result.exit_code == 0, result.output
    mock_apply.assert_called_once()
    lines = result.stdout.splitlines()
    note = lines.index(
        f"The unit is running the supervisor for {old.resolve()}. Installing does not restart "
        "it, and systemd applies the new ExecStart, WorkingDirectory only when the unit next "
        "starts, so that supervisor keeps running until then."
    )
    assert lines[note + 1 : note + 7] == [
        "To let its in-flight tasks finish, then switch, run:",
        f"  claude-task-runner supervisor drain --queue {old.resolve()}",
        "  systemctl --user start claude-task-runner",
        "To switch at once, run:",
        "  systemctl --user restart claude-task-runner",
        "",
    ]
    assert note < next(i for i, line in enumerate(lines) if "Apply this change?" in line)
    # The answer to the prompt is not echoed, so the prompt shares the last line.
    assert lines[-1].endswith(
        "? [y/n] (n): systemd unit installed. Its running supervisor keeps the old command "
        "until the unit restarts (see above)."
    )


def test_install_systemd_says_to_restart_for_a_new_command(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Same queue, another executable, as after reinstalling into a new venv."""
    queue = tmp_path / "queue"
    queue.mkdir()
    _write_installed_unit(queue.resolve(), exe="/old/venv/bin/claude-task-runner")
    with _systemd_install(active=True):
        result = runner.invoke(app, ["--yes", "--queue", str(queue)])
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    note = lines.index(
        "The unit is running. Installing does not restart it, and systemd applies the new "
        "ExecStart only when the unit next starts. To apply it now, run:"
    )
    assert lines[note + 1] == "  systemctl --user restart claude-task-runner"


def test_install_systemd_policy_change_needs_no_restart(runner: CliRunner, tmp_path: Path) -> None:
    """systemd 255 applies RestartSec and StartLimit* at daemon-reload, so nothing is asked."""
    queue = tmp_path / "queue"
    queue.mkdir()
    config = queue.resolve() / "claude_runner.toml"
    config.write_text("[watchdog]\nrestart_cooldown_s = 7\n", encoding="utf-8")
    # Installed before the TOML changed: the same command, the old policy.
    _write_installed_unit(queue.resolve(), config=config)
    with _systemd_install(active=None):
        result = runner.invoke(app, ["--yes", "--queue", str(queue)])
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[-1] == "systemd unit installed and started."


def test_install_systemd_task_cap_change_needs_no_restart(
    runner: CliRunner, tmp_path: Path
) -> None:
    """systemd 255 applies a changed TimeoutStopSec at daemon-reload, so nothing is asked.

    A throwaway unit started with ``TimeoutStopSec=60``, reloaded with 3,
    stopped in 3 s."""
    queue = tmp_path / "queue"
    queue.mkdir()
    config = queue.resolve() / "claude_runner.toml"
    config.write_text(
        "[supervisor]\nadopt_workers = false\n\n[task_caps]\nmax_duration_s_per_task = 28800\n",
        encoding="utf-8",
    )
    # Installed before the cap changed: the same command, TimeoutStopSec=14400.
    _write_installed_unit(queue.resolve(), config=config, adopt_workers=False)
    with _systemd_install(active=None) as mock_apply:
        result = runner.invoke(app, ["--yes", "--queue", str(queue)])
    assert result.exit_code == 0, result.output
    unit_lines = _written_unit_lines(mock_apply)
    assert [ln for ln in unit_lines if ln.startswith("TimeoutStopSec=")] == ["TimeoutStopSec=28800"]
    assert "The unit is running" not in result.stdout
    assert result.stdout.splitlines()[-1] == "systemd unit installed and started."


def test_install_systemd_stopped_unit_starts_with_the_new_unit(
    runner: CliRunner, tmp_path: Path
) -> None:
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    _write_installed_unit(old.resolve())
    with _systemd_install(active=False):
        result = runner.invoke(app, ["--yes", "--queue", str(new)])
    assert result.exit_code == 0, result.output
    assert "The unit is running" not in result.stdout
    assert result.stdout.splitlines()[-1] == "systemd unit installed and started."


def test_install_systemd_abort_after_the_running_unit_note(
    runner: CliRunner, tmp_path: Path
) -> None:
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    _write_installed_unit(old.resolve())
    with _systemd_install(active=True) as mock_apply:
        result = runner.invoke(app, ["--queue", str(new)], input="n\n")
    assert result.exit_code == 1
    mock_apply.assert_not_called()
    assert f"The unit is running the supervisor for {old.resolve()}." in result.stdout


# ---------------------------------------------------------------------------
# `install` — cron branch
# ---------------------------------------------------------------------------


def _cron_plan_mock(*, block_existed: bool = False, diff_lines: list[str] | None = None) -> Any:
    return MagicMock(
        block_existed=block_existed,
        diff_lines=diff_lines if diff_lines is not None else ["+ * * * * /watchdog.sh"],
        existing_text="",
    )


def test_install_cron_happy_path(runner: CliRunner, tmp_path: Path) -> None:
    with (
        patch(
            "claude_task_runner.cli.install_cmd._detect_init_system",
            return_value="cron",
        ),
        patch(
            "claude_task_runner.cli.install_cmd.cron_install.build_install_plan",
            return_value=_cron_plan_mock(),
        ),
        patch(
            "claude_task_runner.cli.install_cmd.cron_install.backup_crontab",
            return_value=tmp_path / "bk.txt",
        ),
        patch("claude_task_runner.cli.install_cmd.cron_install.apply_plan") as mock_apply,
    ):
        result = runner.invoke(app, ["--yes", "--queue", str(tmp_path)])
    assert result.exit_code == 0
    mock_apply.assert_called_once()
    assert "crontab updated" in result.stdout


def test_install_cron_aborts_on_no(runner: CliRunner, tmp_path: Path) -> None:
    with (
        patch(
            "claude_task_runner.cli.install_cmd._detect_init_system",
            return_value="cron",
        ),
        patch(
            "claude_task_runner.cli.install_cmd.cron_install.build_install_plan",
            return_value=_cron_plan_mock(),
        ),
        patch("claude_task_runner.cli.install_cmd.cron_install.apply_plan") as mock_apply,
    ):
        result = runner.invoke(app, ["--queue", str(tmp_path)], input="n\n")
    assert result.exit_code == 1
    mock_apply.assert_not_called()


def test_install_cron_apply_failure(runner: CliRunner, tmp_path: Path) -> None:
    from claude_task_runner.cron.install import CrontabError

    with (
        patch(
            "claude_task_runner.cli.install_cmd._detect_init_system",
            return_value="cron",
        ),
        patch(
            "claude_task_runner.cli.install_cmd.cron_install.build_install_plan",
            return_value=_cron_plan_mock(),
        ),
        patch(
            "claude_task_runner.cli.install_cmd.cron_install.backup_crontab",
            return_value=tmp_path / "bk.txt",
        ),
        patch(
            "claude_task_runner.cli.install_cmd.cron_install.apply_plan",
            side_effect=CrontabError("crontab not installed"),
        ),
    ):
        result = runner.invoke(app, ["--yes", "--queue", str(tmp_path)])
    assert result.exit_code == 2
    assert "crontab install failed" in result.stdout


def test_install_cron_empty_diff_branch(runner: CliRunner, tmp_path: Path) -> None:
    """When diff_lines is empty, the up-to-date branch fires."""
    with (
        patch(
            "claude_task_runner.cli.install_cmd._detect_init_system",
            return_value="cron",
        ),
        patch(
            "claude_task_runner.cli.install_cmd.cron_install.build_install_plan",
            return_value=_cron_plan_mock(diff_lines=[], block_existed=True),
        ),
        patch(
            "claude_task_runner.cli.install_cmd.cron_install.backup_crontab",
            return_value=tmp_path / "bk.txt",
        ),
        patch("claude_task_runner.cli.install_cmd.cron_install.apply_plan"),
    ):
        result = runner.invoke(app, ["--yes", "--queue", str(tmp_path)])
    assert result.exit_code == 0
    assert "up to date" in result.stdout


# ---------------------------------------------------------------------------
# `install` — cron branch registers the queue with the watchdog
# ---------------------------------------------------------------------------


@contextmanager
def _cron_install_patched(backup_path: Path) -> Iterator[MagicMock]:
    """Patch out the crontab I/O of the cron branch; yield the ``apply_plan`` mock."""
    with (
        patch(
            "claude_task_runner.cli.install_cmd._detect_init_system",
            return_value="cron",
        ),
        patch(
            "claude_task_runner.cli.install_cmd.cron_install.build_install_plan",
            return_value=_cron_plan_mock(),
        ),
        patch(
            "claude_task_runner.cli.install_cmd.cron_install.backup_crontab",
            return_value=backup_path,
        ),
        patch("claude_task_runner.cli.install_cmd.cron_install.apply_plan") as mock_apply,
    ):
        yield mock_apply


def test_install_cron_registers_queue_with_watchdog(runner: CliRunner, tmp_path: Path) -> None:
    """Regression: a cron install must register its queue with the watchdog.

    The crontab line runs ``watchdog.sh``, which runs ``watchdog tick``
    with no ``--queue``; ``tick`` manages only the queues listed in
    ``~/.claude_task_runner/queues.json``. ``install`` used to leave that
    registry untouched, so until the operator also ran ``watchdog
    register`` every tick logged "no queues registered; nothing to do"
    and a dead supervisor was never restarted."""
    queue = tmp_path / "queue"
    queue.mkdir()
    with _cron_install_patched(tmp_path / "bk.txt") as mock_apply:
        result = runner.invoke(app, ["--yes", "--queue", str(queue)])
    assert result.exit_code == 0, result.output
    mock_apply.assert_called_once()
    assert load_registered_queues() == [queue.resolve()]


def test_cron_install_then_tick_manages_the_queue(runner: CliRunner, tmp_path: Path) -> None:
    """End to end: the tick the crontab line runs sees the installed queue.

    Before the fix this tick printed "no queues registered; nothing to
    do". With no supervisor running, it must now decide to restart one
    for the queue ``install`` was given."""
    queue = tmp_path / "queue"
    queue.mkdir()
    with _cron_install_patched(tmp_path / "bk.txt"):
        installed = runner.invoke(app, ["--yes", "--queue", str(queue)])
    assert installed.exit_code == 0, installed.output

    ticked = runner.invoke(watchdog_cmd.app, ["tick", "--dry-run"])
    assert ticked.exit_code == 0, ticked.output
    assert "no queues registered" not in ticked.stdout
    assert f"watchdog queue={queue.resolve()} alive=False pid=None verdict=restart" in (
        ticked.stdout
    )


def test_install_cron_shows_registration_before_confirming(
    runner: CliRunner, tmp_path: Path
) -> None:
    """The y/N prompt covers the registry write as well as the crontab diff."""
    queue = tmp_path / "queue"
    queue.mkdir()
    with _cron_install_patched(tmp_path / "bk.txt"):
        result = runner.invoke(app, ["--queue", str(queue)], input="y\n")
    assert result.exit_code == 0, result.output
    shown = result.stdout.index("Will register this queue with the watchdog")
    assert shown < result.stdout.index("Apply this change?")
    assert load_registered_queues() == [queue.resolve()]


def test_install_cron_abort_does_not_register(runner: CliRunner, tmp_path: Path) -> None:
    queue = tmp_path / "queue"
    queue.mkdir()
    with _cron_install_patched(tmp_path / "bk.txt") as mock_apply:
        result = runner.invoke(app, ["--queue", str(queue)], input="n\n")
    assert result.exit_code == 1
    mock_apply.assert_not_called()
    assert not queues_registry_path().exists()


def test_install_cron_rerun_registers_queue_once(runner: CliRunner, tmp_path: Path) -> None:
    queue = tmp_path / "queue"
    queue.mkdir()
    for _ in range(2):
        with _cron_install_patched(tmp_path / "bk.txt"):
            result = runner.invoke(app, ["--yes", "--queue", str(queue)])
        assert result.exit_code == 0, result.output
    assert load_registered_queues() == [queue.resolve()]


def test_install_cron_registry_write_failure_leaves_crontab_untouched(
    runner: CliRunner, tmp_path: Path, isolated_home: Path
) -> None:
    """A registry that cannot be written aborts before the crontab changes.

    Here ``~/.claude_task_runner`` is a file, so creating the registry
    fails with a real ``FileExistsError``. Installing the cron line
    anyway would recreate the bug: a watchdog with no queue to manage."""
    (isolated_home / ".claude_task_runner").write_text("", encoding="utf-8")
    queue = tmp_path / "queue"
    queue.mkdir()
    with (
        patch(
            "claude_task_runner.cli.install_cmd._detect_init_system",
            return_value="cron",
        ),
        patch(
            "claude_task_runner.cli.install_cmd.cron_install.build_install_plan",
            return_value=_cron_plan_mock(),
        ),
        patch("claude_task_runner.cli.install_cmd.cron_install.backup_crontab") as mock_backup,
        patch("claude_task_runner.cli.install_cmd.cron_install.apply_plan") as mock_apply,
    ):
        result = runner.invoke(app, ["--yes", "--queue", str(queue)])
    assert result.exit_code == 2
    assert "watchdog registration failed" in result.stdout
    mock_backup.assert_not_called()
    mock_apply.assert_not_called()


def test_install_cron_replaces_the_registered_queue(runner: CliRunner, tmp_path: Path) -> None:
    """One supervisor runs per user, so the watchdog manages one queue.

    A second cron install used to add its queue beside the first. Every
    tick then spawned the second queue's supervisor, and every spawn
    exited on the lock that the first queue's supervisor held. The y/N
    prompt names the queue being replaced."""
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    register_queue(old)
    with _cron_install_patched(tmp_path / "bk.txt"):
        result = runner.invoke(app, ["--queue", str(new)], input="y\n")
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    shown = lines.index("It replaces, since the watchdog manages one queue:")
    assert lines[shown + 1] == f"  {old.resolve()}"
    assert shown < next(i for i, line in enumerate(lines) if "Apply this change?" in line)
    assert load_registered_queues() == [new.resolve()]
    # The lock is free, so the next tick starts the new queue's supervisor.
    assert lines[-1] == "crontab updated."


def test_install_cron_lists_each_queue_an_older_registry_held(
    runner: CliRunner, tmp_path: Path
) -> None:
    a, b, new = tmp_path / "a", tmp_path / "b", tmp_path / "new"
    new.mkdir()
    registry = queues_registry_path()
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({"queues": [str(a), str(new), str(b), str(a)]}))
    with _cron_install_patched(tmp_path / "bk.txt"):
        result = runner.invoke(app, ["--yes", "--queue", str(new)])
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    shown = lines.index("It replaces, since the watchdog manages one queue:")
    assert lines[shown + 1 : shown + 3] == [f"  {a}", f"  {b}"]
    assert load_registered_queues() == [new.resolve()]


def test_install_cron_rerun_replaces_nothing(runner: CliRunner, tmp_path: Path) -> None:
    queue = tmp_path / "queue"
    queue.mkdir()
    register_queue(queue)
    with _cron_install_patched(tmp_path / "bk.txt"):
        result = runner.invoke(app, ["--yes", "--queue", str(queue)])
    assert result.exit_code == 0, result.output
    assert "It replaces" not in result.stdout


def test_install_cron_says_how_to_hand_over_from_the_running_supervisor(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Ticks start none while the replaced queue's supervisor holds the lock."""
    old, new = tmp_path / "old", tmp_path / "new"
    (old / ".claude_task_runner").mkdir(parents=True)
    new.mkdir()
    register_queue(old)
    (old / ".claude_task_runner" / "supervisor.pid").write_text(f"{os.getpid()}\n")
    with acquire_global_lock(), _cron_install_patched(tmp_path / "bk.txt"):
        result = runner.invoke(app, ["--yes", "--queue", str(new)])
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[-2:] == [
        "crontab updated.",
        f"The supervisor for {old.resolve()} (pid {os.getpid()}) still holds global.lock, "
        "so the watchdog starts this queue's supervisor once it exits. To hand over now, "
        f"run: claude-task-runner supervisor drain --queue {old.resolve()}",
    ]


def test_install_cron_shows_it_replaces_an_unreadable_registry(
    runner: CliRunner, tmp_path: Path
) -> None:
    """The registry is only read before the prompt; registering backs it up."""
    registry = queues_registry_path()
    registry.parent.mkdir(parents=True)
    registry.write_text("{not json", encoding="utf-8")
    queue = tmp_path / "queue"
    queue.mkdir()
    with _cron_install_patched(tmp_path / "bk.txt"):
        result = runner.invoke(app, ["--queue", str(queue)], input="y\n")
    assert result.exit_code == 0, result.output
    shown = next(
        line for line in result.stdout.splitlines() if line.startswith("It replaces the registry")
    )
    assert shown.startswith(
        f"It replaces the registry, which is unreadable (corrupt queues registry at {registry} ("
    )
    assert shown.endswith("); a copy is kept as queues.json.broken.")
    assert load_registered_queues() == [queue.resolve()]
    assert (registry.parent / "queues.json.broken").read_text(encoding="utf-8") == "{not json"


@pytest.mark.parametrize("init_system", ["systemd", "cron"])
def test_install_missing_queue_dir_fails_before_any_change(
    runner: CliRunner, tmp_path: Path, init_system: str
) -> None:
    """A typo'd ``--queue`` fails before any plan is shown or written.

    The cron branch used to show its diff, ask to confirm, and only then
    fail to register the queue. The systemd branch wrote and started a unit
    for it. Registered, the next tick's restart would create the directory
    and start a supervisor on an empty queue, which would hold the per-user
    global lock."""
    missing = tmp_path / "no-such-queue"
    with (
        patch(
            "claude_task_runner.cli.install_cmd._detect_init_system",
            return_value=init_system,
        ),
        patch("claude_task_runner.cli.install_cmd.systemd_mod.build_install_plan") as sd_plan,
        patch("claude_task_runner.cli.install_cmd.systemd_mod.apply_plan") as sd_apply,
        patch("claude_task_runner.cli.install_cmd.cron_install.build_install_plan") as cron_plan,
        patch("claude_task_runner.cli.install_cmd.cron_install.apply_plan") as cron_apply,
        patch(
            "claude_task_runner.cli.install_cmd.shutil.which",
            return_value="/usr/local/bin/claude-task-runner",
        ),
    ):
        result = runner.invoke(app, ["--yes", "--queue", str(missing)])
    assert result.exit_code == 2
    assert result.stdout == f"--queue is not an existing directory: {missing.resolve()}\n"
    for mock in (sd_plan, sd_apply, cron_plan, cron_apply):
        mock.assert_not_called()
    assert not missing.exists()
    assert not queues_registry_path().exists()


def test_install_systemd_does_not_register_queue(runner: CliRunner, tmp_path: Path) -> None:
    """systemd restarts its own unit; the cron watchdog's registry stays empty."""
    with (
        patch(
            "claude_task_runner.cli.install_cmd._detect_init_system",
            return_value="systemd",
        ),
        patch(
            "claude_task_runner.cli.install_cmd.systemd_mod.build_install_plan",
            return_value=_systemd_plan_mock(unit_path=tmp_path / "ctr.service"),
        ),
        patch("claude_task_runner.cli.install_cmd.systemd_mod.apply_plan") as mock_apply,
        patch(
            "claude_task_runner.cli.install_cmd.shutil.which",
            return_value="/usr/local/bin/claude-task-runner",
        ),
    ):
        result = runner.invoke(app, ["--yes", "--queue", str(tmp_path)])
    assert result.exit_code == 0, result.output
    mock_apply.assert_called_once()
    assert not queues_registry_path().exists()


# ---------------------------------------------------------------------------
# `install` — where the queue's [watchdog] settings and --config go
# ---------------------------------------------------------------------------


@contextmanager
def _systemd_install_patched() -> Iterator[MagicMock]:
    """Patch the systemd branch's I/O, but not its plan; yield the ``apply_plan`` mock.

    The real ``build_install_plan`` runs, so the unit text is what
    ``install`` would write, at ``$HOME/.config/systemd/user``."""
    with (
        patch(
            "claude_task_runner.cli.install_cmd._detect_init_system",
            return_value="systemd",
        ),
        patch(
            "claude_task_runner.cli.install_cmd.shutil.which",
            return_value="/usr/local/bin/claude-task-runner",
        ),
        patch("claude_task_runner.cli.install_cmd.systemd_mod.apply_plan") as mock_apply,
    ):
        yield mock_apply


def _written_unit_lines(mock_apply: MagicMock) -> list[str]:
    mock_apply.assert_called_once()
    lines: list[str] = mock_apply.call_args.args[0].unit_text.splitlines()
    return lines


def test_install_systemd_unit_takes_the_watchdog_table(runner: CliRunner, tmp_path: Path) -> None:
    """The queue's ``[watchdog]`` sets the unit's restart policy.

    Before, the unit got ``RestartSec=30``, ``StartLimitBurst=5`` and
    ``StartLimitIntervalSec=600`` whatever the TOML said."""
    queue = tmp_path / "queue"
    queue.mkdir()
    (queue / "claude_runner.toml").write_text(
        "[watchdog]\n"
        "restart_cooldown_s = 120\n"
        "restart_backoff_max_s = 1800\n"
        "crash_loop_threshold = 9\n",
        encoding="utf-8",
    )
    with _systemd_install_patched() as mock_apply:
        result = runner.invoke(app, ["--yes", "--queue", str(queue)])
    assert result.exit_code == 0, result.output
    unit_lines = _written_unit_lines(mock_apply)
    for line in ("RestartSec=120", "StartLimitBurst=9", "StartLimitIntervalSec=1800"):
        assert line in unit_lines
        # The operator sees it in the unit text before confirming.
        assert f"  {line}\n" in result.stdout


def test_install_systemd_unit_without_a_watchdog_table_keeps_the_old_policy(
    runner: CliRunner, tmp_path: Path
) -> None:
    queue = tmp_path / "queue"
    queue.mkdir()
    with _systemd_install_patched() as mock_apply:
        result = runner.invoke(app, ["--yes", "--queue", str(queue)])
    assert result.exit_code == 0, result.output
    unit_lines = _written_unit_lines(mock_apply)
    assert "RestartSec=30" in unit_lines
    assert "StartLimitBurst=5" in unit_lines
    assert "StartLimitIntervalSec=600" in unit_lines


def _install_with_task_cap(
    runner: CliRunner, tmp_path: Path, toml: str
) -> tuple[Result, MagicMock]:
    """Run a systemd ``install --yes`` for a queue whose ``claude_runner.toml`` is ``toml``."""
    queue = tmp_path / "queue"
    queue.mkdir()
    (queue / "claude_runner.toml").write_text(toml, encoding="utf-8")
    with _systemd_install_patched() as mock_apply:
        result = runner.invoke(app, ["--yes", "--queue", str(queue)])
    return result, mock_apply


def _timeout_stop_lines(mock_apply: MagicMock) -> list[str]:
    return [ln for ln in _written_unit_lines(mock_apply) if ln.startswith("TimeoutStopSec=")]


def _drain_toml(cap: str) -> str:
    """A ``claude_runner.toml`` with adoption off and a task cap of ``cap``."""
    return f"[supervisor]\nadopt_workers = false\n\n[task_caps]\nmax_duration_s_per_task = {cap}\n"


@pytest.mark.parametrize(
    ("cap", "timeout"),
    [("0", "infinity"), ("14400", "14400"), ("28800", "28800"), ("3600.5", "3600.5")],
)
def test_install_systemd_drain_unit_waits_for_the_duration_cap(
    runner: CliRunner, tmp_path: Path, cap: str, timeout: str
) -> None:
    """With ``[supervisor].adopt_workers`` off, a stop waits as long as the cap lets a task run.

    ``systemctl --user stop`` waits ``TimeoutStopSec`` for in-flight tasks,
    then SIGKILLs the supervisor. ``install`` used to write 14400 whatever
    ``[task_caps].max_duration_s_per_task`` said, so a stop killed tasks
    that a longer cap, or no cap (0), let run."""
    result, mock_apply = _install_with_task_cap(runner, tmp_path, _drain_toml(cap))
    assert result.exit_code == 0, result.output
    assert _timeout_stop_lines(mock_apply) == [f"TimeoutStopSec={timeout}"]
    # The operator sees it in the unit text before confirming.
    assert f"  TimeoutStopSec={timeout}\n" in result.stdout


def test_install_systemd_refuses_a_duration_cap_the_unit_cannot_carry(
    runner: CliRunner, tmp_path: Path, isolated_home: Path
) -> None:
    """systemd would read the cap as no timeout and wait forever.

    A cap longer than systemd accepts no longer loads: the schema's
    ten-year ceiling refuses it first. The message keeps ``[task_caps]``,
    which Rich markup would otherwise take for a style tag and drop."""
    result, mock_apply = _install_with_task_cap(runner, tmp_path, _drain_toml("1e-07"))
    assert result.exit_code == 2
    assert (
        "systemd install failed: [task_caps].max_duration_s_per_task = 1e-07 rounds to 0 "
        "at systemd's resolution of one microsecond, and systemd reads TimeoutStopSec=0 "
        "as no timeout. Nothing was written.\n"
    ) in result.stdout
    assert "Unit text:" not in result.stdout
    mock_apply.assert_not_called()
    assert not (isolated_home / ".config").exists()


@pytest.mark.parametrize("cap", ["0", "1e-07", "28800", "315360000"])
def test_install_systemd_fast_stop_unit_ignores_the_duration_cap(
    runner: CliRunner, tmp_path: Path, cap: str
) -> None:
    """With adoption on, the default, a stop leaves the workers running (ADR-0025).

    The supervisor exits at once, so the unit waits 30 s whatever the cap,
    even one the drain unit refuses."""
    result, mock_apply = _install_with_task_cap(
        runner, tmp_path, f"[task_caps]\nmax_duration_s_per_task = {cap}\n"
    )
    assert result.exit_code == 0, result.output
    assert _timeout_stop_lines(mock_apply) == ["TimeoutStopSec=30"]


def test_install_systemd_refuses_a_watchdog_value_systemd_cannot_parse(
    runner: CliRunner, tmp_path: Path, isolated_home: Path
) -> None:
    """systemd would ignore the line and fall back to its own default burst.

    The schema puts no upper bound on the count, so it loads. The message
    keeps ``[watchdog]``, which Rich markup would otherwise take for a style
    tag and drop."""
    queue = tmp_path / "queue"
    queue.mkdir()
    (queue / "claude_runner.toml").write_text(
        "[watchdog]\ncrash_loop_threshold = 4294967296\n", encoding="utf-8"
    )
    with _systemd_install_patched() as mock_apply:
        result = runner.invoke(app, ["--yes", "--queue", str(queue)])
    assert result.exit_code == 2
    assert (
        "systemd install failed: [watchdog].crash_loop_threshold = 4294967296 is more "
        "than systemd accepts (4294967295). Nothing was written.\n"
    ) in result.stdout
    assert "Unit text:" not in result.stdout
    mock_apply.assert_not_called()
    assert not (isolated_home / ".config").exists()


@pytest.mark.parametrize(
    ("table", "key", "value"),
    [
        ("watchdog", "crash_loop_threshold", "0"),
        # Not finite, so systemd could not parse it either.
        ("watchdog", "restart_cooldown_s", "inf"),
        # Past the schema's ten-year ceiling.
        ("watchdog", "restart_cooldown_s", "315360001"),
        # Past the ceiling, and longer than systemd accepts.
        ("task_caps", "max_duration_s_per_task", "18446744073709"),
    ],
)
def test_install_systemd_value_the_schema_rejects_fails_before_writing(
    runner: CliRunner, tmp_path: Path, table: str, key: str, value: str
) -> None:
    """A value the schema rejects stops ``install`` when the TOML loads."""
    queue = tmp_path / "queue"
    queue.mkdir()
    (queue / "claude_runner.toml").write_text(f"[{table}]\n{key} = {value}\n", encoding="utf-8")
    with _systemd_install_patched() as mock_apply:
        result = runner.invoke(app, ["--yes", "--queue", str(queue)])
    assert result.exit_code == 1
    assert isinstance(result.exception, ConfigError)
    assert f"{table}.{key}" in str(result.exception)
    mock_apply.assert_not_called()


def test_install_systemd_writes_the_config_it_checked_as_an_absolute_path(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative ``--config`` must name the same file inside the unit.

    ``install`` loads the TOML relative to the directory it runs in, but
    the unit runs with ``WorkingDirectory=<queue>``. The relative path
    used to go into ExecStart and ExecStop as given, so the supervisor
    looked for ``<queue>/rel.toml``, a file that install never checked
    and that usually does not exist."""
    queue = tmp_path / "queue"
    queue.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    (work / "rel.toml").write_text("[watchdog]\nrestart_cooldown_s = 45\n", encoding="utf-8")
    monkeypatch.chdir(work)
    checked = Path.cwd() / "rel.toml"
    with _systemd_install_patched() as mock_apply:
        result = runner.invoke(app, ["--yes", "--queue", str(queue), "--config", "rel.toml"])
    assert result.exit_code == 0, result.output
    unit_lines = _written_unit_lines(mock_apply)
    exe = "/usr/local/bin/claude-task-runner"
    queue_flag = f"--queue {queue.resolve()}"
    assert f"ExecStart={exe} supervisor start {queue_flag} --config {checked}" in unit_lines
    assert f"ExecStop=-{exe} supervisor stop {queue_flag} --config {checked}" in unit_lines
    # The TOML that install loaded is the one whose [watchdog] the unit carries.
    assert "RestartSec=45" in unit_lines


def _record_tick_spawns(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, Path | None]]:
    """Replace the tick's spawn with a recorder of (queue, --config)."""
    spawned: list[tuple[Path, Path | None]] = []

    def _record(queue_dir: Path, config: Path | None = None) -> int:
        spawned.append((queue_dir, config))
        return 4242

    monkeypatch.setattr(watchdog_cmd, "_spawn_supervisor", _record)
    return spawned


def test_install_cron_records_its_config(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cron ``install --config`` is recorded, and the tick uses that file.

    Before, the registry kept only the queue's path, so the tick decided
    with the package defaults and spawned ``supervisor start`` without
    ``--config``, and that supervisor found only
    ``<queue>/claude_runner.toml``."""
    queue = tmp_path / "queue"
    queue.mkdir()
    config = tmp_path / "elsewhere" / "custom.toml"
    config.parent.mkdir()
    config.write_text("[watchdog]\ncrash_loop_threshold = 7\n", encoding="utf-8")
    with _cron_install_patched(tmp_path / "bk.txt"):
        installed = runner.invoke(
            app, ["--queue", str(queue), "--config", str(config)], input="y\n"
        )
    assert installed.exit_code == 0, installed.output
    # Shown under the queue it is recorded for, before the y/N prompt.
    shown = installed.stdout.index(f"  {queue.resolve()}\n  with config {config}\n")
    assert shown < installed.stdout.index("Apply this change?")
    registry = json.loads(queues_registry_path().read_text(encoding="utf-8"))
    assert registry == {
        "queues": [str(queue.resolve())],
        "configs": {str(queue.resolve()): str(config)},
    }

    spawned = _record_tick_spawns(monkeypatch)
    ticked = runner.invoke(watchdog_cmd.app, ["tick"])
    assert ticked.exit_code == 0, ticked.output
    assert "detail='restart approved (recent count: 1 of threshold 7)'" in ticked.stdout
    assert spawned == [(queue.resolve(), config)]


def test_install_cron_without_config_records_none(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The queue's own TOML is found at each tick, as ``supervisor start`` finds it."""
    queue = tmp_path / "queue"
    queue.mkdir()
    toml = queue / "claude_runner.toml"
    toml.write_text("[watchdog]\ncrash_loop_threshold = 9\n", encoding="utf-8")
    with _cron_install_patched(tmp_path / "bk.txt"):
        installed = runner.invoke(app, ["--yes", "--queue", str(queue)])
    assert installed.exit_code == 0, installed.output
    assert "with config" not in installed.stdout
    registry = json.loads(queues_registry_path().read_text(encoding="utf-8"))
    assert registry == {"queues": [str(queue.resolve())]}

    spawned = _record_tick_spawns(monkeypatch)
    ticked = runner.invoke(watchdog_cmd.app, ["tick"])
    assert ticked.exit_code == 0, ticked.output
    assert "detail='restart approved (recent count: 1 of threshold 9)'" in ticked.stdout
    assert spawned == [(queue.resolve(), toml.resolve())]


def test_install_cron_rerun_without_config_drops_the_recorded_one(
    runner: CliRunner, tmp_path: Path
) -> None:
    queue = tmp_path / "queue"
    queue.mkdir()
    config = tmp_path / "custom.toml"
    config.write_text("", encoding="utf-8")
    for args in (["--config", str(config)], []):
        with _cron_install_patched(tmp_path / "bk.txt"):
            result = runner.invoke(app, ["--yes", "--queue", str(queue), *args])
        assert result.exit_code == 0, result.output
    registry = json.loads(queues_registry_path().read_text(encoding="utf-8"))
    assert registry == {"queues": [str(queue.resolve())]}


def test_install_cron_records_a_relative_config_as_absolute(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tick runs in cron's working directory, not the one install ran in."""
    queue = tmp_path / "queue"
    queue.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    (work / "rel.toml").write_text("", encoding="utf-8")
    monkeypatch.chdir(work)
    with _cron_install_patched(tmp_path / "bk.txt"):
        result = runner.invoke(app, ["--yes", "--queue", str(queue), "--config", "rel.toml"])
    assert result.exit_code == 0, result.output
    registry = json.loads(queues_registry_path().read_text(encoding="utf-8"))
    assert registry["configs"] == {str(queue.resolve()): str(Path.cwd() / "rel.toml")}


# ---------------------------------------------------------------------------
# `uninstall`
# ---------------------------------------------------------------------------


def test_uninstall_systemd_unit_present(runner: CliRunner, tmp_path: Path) -> None:
    fake_unit = tmp_path / "ctr.service"
    fake_unit.write_text("", encoding="utf-8")
    with (
        patch(
            "claude_task_runner.cli.install_cmd._detect_init_system",
            return_value="systemd",
        ),
        patch(
            "claude_task_runner.cli.install_cmd.systemd_mod.systemd_unit_path",
            return_value=fake_unit,
        ),
        patch(
            "claude_task_runner.cli.install_cmd.systemd_mod.uninstall",
            return_value=True,
        ),
        patch(
            "claude_task_runner.cli.install_cmd.cron_install.build_uninstall_plan",
            return_value=MagicMock(block_existed=False),
        ),
    ):
        result = runner.invoke(app, ["uninstall", "--yes"])
    assert result.exit_code == 0
    assert "systemd unit removed" in result.stdout


def test_uninstall_systemd_unit_missing(runner: CliRunner, tmp_path: Path) -> None:
    with (
        patch(
            "claude_task_runner.cli.install_cmd._detect_init_system",
            return_value="systemd",
        ),
        patch(
            "claude_task_runner.cli.install_cmd.systemd_mod.systemd_unit_path",
            return_value=tmp_path / "does-not-exist.service",
        ),
        patch(
            "claude_task_runner.cli.install_cmd.cron_install.build_uninstall_plan",
            return_value=MagicMock(block_existed=False),
        ),
    ):
        result = runner.invoke(app, ["uninstall", "--yes"])
    assert result.exit_code == 0
    assert "No systemd unit installed" in result.stdout


def test_uninstall_systemd_aborts_on_no(runner: CliRunner, tmp_path: Path) -> None:
    fake_unit = tmp_path / "ctr.service"
    fake_unit.write_text("", encoding="utf-8")
    with (
        patch(
            "claude_task_runner.cli.install_cmd._detect_init_system",
            return_value="systemd",
        ),
        patch(
            "claude_task_runner.cli.install_cmd.systemd_mod.systemd_unit_path",
            return_value=fake_unit,
        ),
        patch(
            "claude_task_runner.cli.install_cmd.systemd_mod.uninstall",
        ) as mock_uninstall,
        patch(
            "claude_task_runner.cli.install_cmd.cron_install.build_uninstall_plan",
            return_value=MagicMock(block_existed=False),
        ),
    ):
        runner.invoke(app, ["uninstall"], input="n\n")
    # Doesn't fail; just notes the skip and moves on to cron.
    mock_uninstall.assert_not_called()


def test_uninstall_cron_no_access(runner: CliRunner) -> None:
    """If `crontab -l` is unavailable, the cron path gracefully skips."""
    from claude_task_runner.cron.install import CrontabError

    with (
        patch(
            "claude_task_runner.cli.install_cmd._detect_init_system",
            return_value="cron",
        ),
        patch(
            "claude_task_runner.cli.install_cmd.cron_install.build_uninstall_plan",
            side_effect=CrontabError("not installed"),
        ),
    ):
        result = runner.invoke(app, ["uninstall", "--yes"])
    assert result.exit_code == 0
    assert "No crontab access" in result.stdout


def test_uninstall_cron_no_block_to_remove(runner: CliRunner) -> None:
    with (
        patch(
            "claude_task_runner.cli.install_cmd._detect_init_system",
            return_value="cron",
        ),
        patch(
            "claude_task_runner.cli.install_cmd.cron_install.build_uninstall_plan",
            return_value=MagicMock(block_existed=False),
        ),
    ):
        result = runner.invoke(app, ["uninstall", "--yes"])
    assert result.exit_code == 0
    assert "nothing to remove" in result.stdout


def test_uninstall_cron_happy_path(runner: CliRunner, tmp_path: Path) -> None:
    plan = MagicMock(
        block_existed=True,
        diff_lines=["- * * * * /watchdog.sh"],
        existing_text="* * * * * /watchdog.sh\n",
    )
    with (
        patch(
            "claude_task_runner.cli.install_cmd._detect_init_system",
            return_value="cron",
        ),
        patch(
            "claude_task_runner.cli.install_cmd.cron_install.build_uninstall_plan",
            return_value=plan,
        ),
        patch(
            "claude_task_runner.cli.install_cmd.cron_install.backup_crontab",
            return_value=tmp_path / "bk.txt",
        ),
        patch("claude_task_runner.cli.install_cmd.cron_install.apply_plan") as mock_apply,
    ):
        result = runner.invoke(app, ["uninstall", "--yes"])
    assert result.exit_code == 0
    mock_apply.assert_called_once()
    assert "crontab block removed" in result.stdout


def test_uninstall_cron_aborts_on_no(runner: CliRunner, tmp_path: Path) -> None:
    plan = MagicMock(
        block_existed=True,
        diff_lines=["- * * * * /watchdog.sh"],
        existing_text="* * * * * /watchdog.sh\n",
    )
    with (
        patch(
            "claude_task_runner.cli.install_cmd._detect_init_system",
            return_value="cron",
        ),
        patch(
            "claude_task_runner.cli.install_cmd.cron_install.build_uninstall_plan",
            return_value=plan,
        ),
        patch(
            "claude_task_runner.cli.install_cmd.cron_install.backup_crontab",
            return_value=tmp_path / "bk.txt",
        ),
        patch("claude_task_runner.cli.install_cmd.cron_install.apply_plan") as mock_apply,
    ):
        result = runner.invoke(app, ["uninstall"], input="n\n")
    assert result.exit_code == 0
    mock_apply.assert_not_called()
    assert "skipped" in result.stdout


def test_uninstall_cron_apply_failure(runner: CliRunner, tmp_path: Path) -> None:
    from claude_task_runner.cron.install import CrontabError

    plan = MagicMock(
        block_existed=True,
        diff_lines=["- * * * * /watchdog.sh"],
        existing_text="* * * * * /watchdog.sh\n",
    )
    with (
        patch(
            "claude_task_runner.cli.install_cmd._detect_init_system",
            return_value="cron",
        ),
        patch(
            "claude_task_runner.cli.install_cmd.cron_install.build_uninstall_plan",
            return_value=plan,
        ),
        patch(
            "claude_task_runner.cli.install_cmd.cron_install.backup_crontab",
            return_value=tmp_path / "bk.txt",
        ),
        patch(
            "claude_task_runner.cli.install_cmd.cron_install.apply_plan",
            side_effect=CrontabError("crontab disappeared"),
        ),
    ):
        result = runner.invoke(app, ["uninstall", "--yes"])
    assert result.exit_code == 2
    assert "cron uninstall failed" in result.stdout


# ---------------------------------------------------------------------------
# `uninstall` — the watchdog registry it leaves behind
# ---------------------------------------------------------------------------


def _block_plan() -> Any:
    return MagicMock(
        block_existed=True,
        diff_lines=["- * * * * /watchdog.sh"],
        existing_text="* * * * * /watchdog.sh\n",
    )


@contextmanager
def _cron_uninstall_patched(plan: Any, backup_path: Path) -> Iterator[MagicMock]:
    """Patch out the crontab I/O of ``uninstall``; yield the ``apply_plan`` mock."""
    with (
        patch(
            "claude_task_runner.cli.install_cmd._detect_init_system",
            return_value="cron",
        ),
        patch(
            "claude_task_runner.cli.install_cmd.cron_install.build_uninstall_plan",
            return_value=plan,
        ),
        patch(
            "claude_task_runner.cli.install_cmd.cron_install.backup_crontab",
            return_value=backup_path,
        ),
        patch("claude_task_runner.cli.install_cmd.cron_install.apply_plan") as mock_apply,
    ):
        yield mock_apply


def _registered(tmp_path: Path, *names: str) -> list[Path]:
    queues = [tmp_path / name for name in names]
    for q in queues:
        q.mkdir()
        register_queue(q)
    return [q.resolve() for q in queues]


def test_uninstall_lists_the_queues_the_registry_still_holds(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Every entry of a list an older version wrote, even one deleted since."""
    queues = [tmp_path / "a", tmp_path / "b"]
    queues[0].mkdir()
    registry = queues_registry_path()
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({"queues": [str(q) for q in queues]}), encoding="utf-8")
    with _cron_uninstall_patched(_block_plan(), tmp_path / "bk.txt") as mock_apply:
        result = runner.invoke(app, ["uninstall", "--yes"])
    assert result.exit_code == 0, result.output
    mock_apply.assert_called_once()
    assert result.stdout.splitlines()[-4:] == [
        "crontab block removed.",
        f"{registry} still lists 2 queues. No tick reads it without the cron block, and a "
        "later cron install replaces the list with its own queue. To drop them now:",
        f"  claude-task-runner watchdog unregister --queue {queues[0]}",
        f"  claude-task-runner watchdog unregister --queue {queues[1]}",
    ]
    # Listed, not removed.
    assert load_registered_queues() == queues


def test_uninstall_without_a_cron_block_lists_the_registry(
    runner: CliRunner, tmp_path: Path
) -> None:
    (queue,) = _registered(tmp_path, "a")
    with _cron_uninstall_patched(MagicMock(block_existed=False), tmp_path / "bk.txt"):
        result = runner.invoke(app, ["uninstall", "--yes"])
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[-2:] == [
        f"{queues_registry_path()} still lists 1 queue. No tick reads it without the cron "
        "block, and a later cron install replaces the list with its own queue. To drop it now:",
        f"  claude-task-runner watchdog unregister --queue {queue}",
    ]


def test_uninstall_that_keeps_the_cron_block_does_not_list_the_registry(
    runner: CliRunner, tmp_path: Path
) -> None:
    """The watchdog still runs, so its queues are in use, not left behind."""
    _registered(tmp_path, "a")
    with _cron_uninstall_patched(_block_plan(), tmp_path / "bk.txt") as mock_apply:
        result = runner.invoke(app, ["uninstall"], input="n\n")
    assert result.exit_code == 0, result.output
    mock_apply.assert_not_called()
    assert "still lists" not in result.stdout


def test_uninstall_without_crontab_access_does_not_list_the_registry(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Whether a cron watchdog still runs is unknown, so nothing is said about its queues."""
    from claude_task_runner.cron.install import CrontabError

    _registered(tmp_path, "a")
    with (
        patch(
            "claude_task_runner.cli.install_cmd._detect_init_system",
            return_value="cron",
        ),
        patch(
            "claude_task_runner.cli.install_cmd.cron_install.build_uninstall_plan",
            side_effect=CrontabError("not installed"),
        ),
    ):
        result = runner.invoke(app, ["uninstall", "--yes"])
    assert result.exit_code == 0, result.output
    assert "still lists" not in result.stdout


def test_uninstall_with_an_empty_registry_says_nothing_about_it(
    runner: CliRunner, tmp_path: Path
) -> None:
    with _cron_uninstall_patched(_block_plan(), tmp_path / "bk.txt"):
        result = runner.invoke(app, ["uninstall", "--yes"])
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[-1] == "crontab block removed."
    assert not queues_registry_path().exists()


def test_uninstall_reports_a_corrupt_registry_and_leaves_it(
    runner: CliRunner, tmp_path: Path
) -> None:
    registry = queues_registry_path()
    registry.parent.mkdir(parents=True)
    registry.write_text("{not json", encoding="utf-8")
    with _cron_uninstall_patched(_block_plan(), tmp_path / "bk.txt"):
        result = runner.invoke(app, ["uninstall", "--yes"])
    assert result.exit_code == 0, result.output
    last = result.stdout.splitlines()[-1]
    assert last.startswith(f"warning: corrupt queues registry at {registry} (")
    assert last.endswith("); uninstall left it as it is.")
    assert registry.read_text(encoding="utf-8") == "{not json"
    assert sorted(p.name for p in registry.parent.iterdir()) == ["queues.json"]
