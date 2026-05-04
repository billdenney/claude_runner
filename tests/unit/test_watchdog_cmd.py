"""Tests for cli.watchdog_cmd — registry + tick decision wiring."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from claude_task_runner.cli import watchdog_cmd
from claude_task_runner.cli.watchdog_cmd import (
    app,
    load_registered_queues,
    queues_registry_path,
    register_queue,
)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``Path.home()`` for every test in this file so we never
    touch the real ``~/.claude_task_runner``."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


class TestRegistry:
    def test_register_and_load(self, isolated_home: Path) -> None:
        queue = isolated_home / "queue1"
        queue.mkdir()
        register_queue(queue)
        out = load_registered_queues()
        assert out == [queue.resolve()]

    def test_register_idempotent(self, isolated_home: Path) -> None:
        queue = isolated_home / "queue1"
        queue.mkdir()
        register_queue(queue)
        register_queue(queue)
        register_queue(queue)
        assert len(load_registered_queues()) == 1

    def test_register_multiple_queues(self, isolated_home: Path) -> None:
        for name in ("a", "b", "c"):
            (isolated_home / name).mkdir()
            register_queue(isolated_home / name)
        out = load_registered_queues()
        assert len(out) == 3

    def test_load_missing_returns_empty(self, isolated_home: Path) -> None:
        assert load_registered_queues() == []

    def test_load_corrupt_returns_empty(self, isolated_home: Path) -> None:
        path = queues_registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        assert load_registered_queues() == []


class TestRegisterCommand:
    def test_register_via_cli(self, runner: CliRunner, isolated_home: Path) -> None:
        queue = isolated_home / "q"
        queue.mkdir()
        result = runner.invoke(app, ["register", "--queue", str(queue)])
        assert result.exit_code == 0
        assert str(queue) in result.stdout
        assert load_registered_queues() == [queue.resolve()]


class TestQueuesCommand:
    def test_queues_lists_registered(self, runner: CliRunner, isolated_home: Path) -> None:
        for name in ("a", "b"):
            (isolated_home / name).mkdir()
            register_queue(isolated_home / name)
        result = runner.invoke(app, ["queues"])
        assert result.exit_code == 0
        assert "a" in result.stdout
        assert "b" in result.stdout


class TestTickCommand:
    def test_tick_with_no_queues_is_safe(self, runner: CliRunner, isolated_home: Path) -> None:
        result = runner.invoke(app, ["tick", "--dry-run"])
        assert result.exit_code == 0
        assert "no queues registered" in result.stdout

    def test_tick_dry_run_does_not_spawn(
        self,
        runner: CliRunner,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        queue = isolated_home / "q"
        (queue / ".claude_task_runner").mkdir(parents=True)
        register_queue(queue)

        # Patch _spawn_supervisor to fail loudly if invoked.
        def _explode(_qd: Path) -> int:
            raise AssertionError("dry-run should not spawn")

        monkeypatch.setattr(watchdog_cmd, "_spawn_supervisor", _explode)
        result = runner.invoke(app, ["tick", "--dry-run"])
        assert result.exit_code == 0
        assert "verdict=restart" in result.stdout

    def test_tick_with_alive_supervisor_skips(
        self,
        runner: CliRunner,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        queue = isolated_home / "q"
        (queue / ".claude_task_runner").mkdir(parents=True)
        register_queue(queue)

        # Pretend the supervisor is alive (use the test's own PID so
        # is_pid_alive returns True).
        import os

        pid_path = queue / ".claude_task_runner" / "supervisor.pid"
        pid_path.write_text(f"{os.getpid()}\n")

        def _explode(_qd: Path) -> int:
            raise AssertionError("alive supervisor should not be respawned")

        monkeypatch.setattr(watchdog_cmd, "_spawn_supervisor", _explode)
        result = runner.invoke(app, ["tick"])
        assert result.exit_code == 0
        assert "verdict=skip" in result.stdout

    def test_tick_handles_corrupt_state(
        self,
        runner: CliRunner,
        isolated_home: Path,
    ) -> None:
        # Pre-corrupt the state file; tick should reset and proceed.
        from claude_task_runner.cron.backoff import watchdog_state_path

        state_path = watchdog_state_path()
        state_path.write_text("{not json")
        result = runner.invoke(app, ["tick", "--dry-run"])
        assert result.exit_code == 0
        assert "bad state file" in result.stdout
