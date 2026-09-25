"""Tests for cli/install_cmd.py — install / uninstall watchdog.

Mocks ``systemctl``, ``crontab``, and any other subprocess invocation
so no real watchdog is ever wired up. Also mocks ``shutil.which`` so
PATH lookups are deterministic regardless of the developer's machine.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from claude_task_runner.cli import watchdog_cmd
from claude_task_runner.cli.install_cmd import (
    _detect_init_system,
    _supervisor_command,
    _watchdog_script_path,
    app,
)
from claude_task_runner.cron.registry import load_registered_queues, queues_registry_path


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
