"""Tests for cron.systemd_unit — --user systemd unit installer."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_task_runner.cron.systemd_unit import (
    UNIT_NAME,
    SystemdError,
    apply_plan,
    build_install_plan,
    build_unit_text,
    is_systemd_user_available,
    uninstall,
)


class TestBuildUnitText:
    def test_includes_required_sections(self) -> None:
        text = build_unit_text(
            supervisor_command="/usr/bin/claude-task-runner supervisor start",
            queue_dir=Path("/queue"),
        )
        assert "[Unit]" in text
        assert "[Service]" in text
        assert "[Install]" in text
        assert "ExecStart=/usr/bin/claude-task-runner supervisor start" in text
        assert "WorkingDirectory=/queue" in text
        assert "Restart=on-failure" in text
        assert "WantedBy=default.target" in text

    def test_clean_exit_does_not_restart(self) -> None:
        text = build_unit_text(
            supervisor_command="/usr/bin/claude-task-runner supervisor start",
            queue_dir=Path("/queue"),
        )
        # Exit-status 0 means STOPPED state; we don't want a relaunch loop.
        assert "RestartPreventExitStatus=0" in text

    def test_restart_sec_customizable(self) -> None:
        text = build_unit_text(
            supervisor_command="x",
            queue_dir=Path("/q"),
            restart_sec_s=120,
        )
        assert "RestartSec=120" in text

    def test_start_limit_customizable(self) -> None:
        text = build_unit_text(
            supervisor_command="x",
            queue_dir=Path("/q"),
            start_limit_burst=10,
        )
        assert "StartLimitBurst=10" in text

    def test_includes_drain_execstop(self) -> None:
        """ExecStop runs ``supervisor drain --no-wait`` so systemctl
        stop/restart goes through the graceful-drain path."""
        text = build_unit_text(
            supervisor_command="/usr/local/bin/claude-task-runner supervisor start --queue /q --config /q/claude_runner.toml",
            queue_dir=Path("/q"),
        )
        assert "ExecStop=" in text
        assert "supervisor drain" in text
        assert "--no-wait" in text
        # Same binary path as ExecStart so the operator's pipx install
        # is honoured.
        assert "ExecStop=/usr/local/bin/claude-task-runner supervisor drain" in text
        # Same --queue / --config so drain targets the right state file.
        assert "--queue /q" in text
        assert "--config /q/claude_runner.toml" in text

    def test_kill_mode_process(self) -> None:
        """KillMode=process so dispatched claude subprocesses survive
        systemd's SIGKILL escalation on the main PID."""
        text = build_unit_text(supervisor_command="x", queue_dir=Path("/q"))
        assert "KillMode=process" in text

    def test_timeout_stop_sec_default_matches_max_task_duration(self) -> None:
        """Default TimeoutStopSec=14400 (4h) matches the default
        [task_caps].max_duration_s_per_task so drain has time to finish
        the longest plausibly-allowed task."""
        text = build_unit_text(supervisor_command="x", queue_dir=Path("/q"))
        assert "TimeoutStopSec=14400" in text

    def test_timeout_stop_sec_customizable(self) -> None:
        text = build_unit_text(
            supervisor_command="x",
            queue_dir=Path("/q"),
            timeout_stop_sec=1800,
        )
        assert "TimeoutStopSec=1800" in text

    def test_clean_drain_exit_does_not_restart(self) -> None:
        """RestartPreventExitStatus=0 — a clean drain-exit means the
        operator asked for stop/restart, NOT a crash. systemd's stop
        sequence handles the eventual fresh-start when needed
        (``systemctl restart`` runs stop then start; ``stop`` alone
        leaves it stopped). Restart=on-failure only fires for crashes."""
        text = build_unit_text(supervisor_command="x", queue_dir=Path("/q"))
        assert "Restart=on-failure" in text
        assert "RestartPreventExitStatus=0" in text


class TestBuildInstallPlan:
    def test_default_path(self, tmp_path: Path) -> None:
        plan = build_install_plan(
            supervisor_command="/usr/bin/claude-task-runner supervisor start",
            queue_dir=Path("/queue"),
            unit_path=tmp_path / f"{UNIT_NAME}.service",
        )
        assert plan.unit_path.name == f"{UNIT_NAME}.service"
        assert plan.block_existed is False
        assert plan.enable_command[0] == "systemctl"
        assert "--user" in plan.enable_command

    def test_block_existed_when_file_present(self, tmp_path: Path) -> None:
        path = tmp_path / f"{UNIT_NAME}.service"
        path.write_text("[Unit]\nDescription=Old\n")
        plan = build_install_plan(
            supervisor_command="/usr/bin/x",
            queue_dir=Path("/q"),
            unit_path=path,
        )
        assert plan.block_existed is True


class TestApplyPlan:
    def _make_fake_systemctl(self, tmp_path: Path, *, fail: bool = False) -> Path:
        if fail:
            body = '#!/usr/bin/env bash\necho "permission denied" 1>&2\nexit 1\n'
        else:
            body = "#!/usr/bin/env bash\nexit 0\n"
        p = tmp_path / "systemctl"
        p.write_text(body)
        p.chmod(0o755)
        return p

    def test_writes_unit_file(self, tmp_path: Path) -> None:
        unit_path = tmp_path / "claude-task-runner.service"
        plan = build_install_plan(
            supervisor_command="/usr/bin/x",
            queue_dir=tmp_path,
            unit_path=unit_path,
        )
        binary = self._make_fake_systemctl(tmp_path)
        apply_plan(plan, systemctl_executable=str(binary))
        assert unit_path.exists()
        assert "ExecStart=/usr/bin/x" in unit_path.read_text()

    def test_apply_propagates_systemctl_failure(self, tmp_path: Path) -> None:
        unit_path = tmp_path / "claude-task-runner.service"
        plan = build_install_plan(
            supervisor_command="/usr/bin/x",
            queue_dir=tmp_path,
            unit_path=unit_path,
        )
        binary = self._make_fake_systemctl(tmp_path, fail=True)
        with pytest.raises(SystemdError):
            apply_plan(plan, systemctl_executable=str(binary))


class TestUninstall:
    def test_removes_existing_unit(self, tmp_path: Path) -> None:
        unit_path = tmp_path / f"{UNIT_NAME}.service"
        unit_path.write_text("[Unit]\nDescription=Test\n")
        existed = uninstall(
            unit_path=unit_path,
            systemctl_executable="/usr/bin/false",  # tolerated failure
        )
        assert existed is True
        assert not unit_path.exists()

    def test_returns_false_when_absent(self, tmp_path: Path) -> None:
        unit_path = tmp_path / f"{UNIT_NAME}.service"
        existed = uninstall(
            unit_path=unit_path,
            systemctl_executable="/usr/bin/false",
        )
        assert existed is False


class TestIsSystemdUserAvailable:
    def test_missing_systemctl_returns_false(self) -> None:
        # An obviously-missing executable name returns False without raising.
        assert is_systemd_user_available(systemctl_executable="this-doesnt-exist-12345") is False

    def test_failing_systemctl_returns_false(self, tmp_path: Path) -> None:
        body = "#!/usr/bin/env bash\nexit 127\n"
        p = tmp_path / "systemctl"
        p.write_text(body)
        p.chmod(0o755)
        assert is_systemd_user_available(systemctl_executable=str(p)) is False


def test_unit_text_includes_term_and_path_environment() -> None:
    """Regression: systemd-user units start with a near-empty environment.
    Without TERM the pexpect-driven `claude /usage` TUI can't render; without
    `~/.local/bin` on PATH `shutil.which("claude")` returns None for pipx
    installs and the supervisor's safe_poll() raises UsageCaptureSpawnError
    forever. The generated unit must inject both.
    """
    text = build_unit_text(
        supervisor_command="/usr/bin/claude-task-runner supervisor start",
        queue_dir=Path("/home/bill/queue"),
    )
    assert "Environment=TERM=" in text
    assert "Environment=PATH=" in text
    # PATH must include the user's local bin so pipx-installed Claude resolves.
    assert "%h/.local/bin" in text or "/.local/bin" in text
