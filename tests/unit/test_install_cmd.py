"""Tests for cli/install_cmd.py — install / uninstall watchdog.

Mocks ``systemctl``, ``crontab``, and any other subprocess invocation
so no real watchdog is ever wired up. Also mocks ``shutil.which`` so
PATH lookups are deterministic regardless of the developer's machine.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from claude_task_runner.cli.install_cmd import (
    _detect_init_system,
    _supervisor_command,
    _watchdog_script_path,
    app,
)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


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
