"""Tests for cli.watchdog_cmd — registry + tick decision wiring."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from claude_task_runner.cli import watchdog_cmd
from claude_task_runner.cli.watchdog_cmd import (
    _spawn_supervisor,
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

    def test_tick_forwards_config_to_spawned_supervisor(
        self,
        runner: CliRunner,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression (audit finding 1): ``tick --config <toml>`` must
        forward ``--config`` to the supervisor it spawns. Without it the
        spawned supervisor falls back to package defaults and its
        throttle/backoff policy silently diverges from the operator's
        ``claude_runner.toml``."""
        queue = isolated_home / "q"
        (queue / ".claude_task_runner").mkdir(parents=True)
        register_queue(queue)

        # A minimal-but-valid config file so ``load_settings`` succeeds.
        config_path = isolated_home / "claude_runner.toml"
        config_path.write_text("", encoding="utf-8")

        spawned: dict[str, int] = {}

        def _record(queue_dir: Path, config: Path | None = None) -> int:
            spawned["called"] = spawned.get("called", 0) + 1
            spawned["config"] = config  # type: ignore[assignment]
            spawned["queue"] = queue_dir  # type: ignore[assignment]
            return 4242

        monkeypatch.setattr(watchdog_cmd, "_spawn_supervisor", _record)
        result = runner.invoke(app, ["tick", "--config", str(config_path)])
        assert result.exit_code == 0, result.output
        assert spawned["called"] == 1
        # The config Path the operator passed must be forwarded verbatim.
        assert spawned["config"] == config_path
        assert "spawned supervisor" in result.stdout


class TestSpawnSupervisor:
    """Direct tests of ``_spawn_supervisor`` argv construction."""

    @staticmethod
    def _popen_capturing_argv(pid: int) -> MagicMock:
        """A ``Popen`` mock that records argv and closes the log file
        handle it's handed.

        The real subprocess inherits ``stdout``/``stderr`` and the OS
        closes them when it exits; the mock never starts a process, so
        without this the ``open(...)`` in ``_spawn_supervisor`` would
        leak and surface as a ``ResourceWarning`` at teardown."""

        def _factory(cmd, *, stdout=None, stderr=None, **_kw):
            if stdout is not None:
                stdout.close()
            proc = MagicMock()
            proc.pid = pid
            return proc

        return MagicMock(side_effect=_factory)

    def test_spawn_appends_config_flag(self, tmp_path: Path) -> None:
        """When a config path is given, the supervisor command line must
        include ``--config <path>`` (audit finding 1)."""
        queue = tmp_path / "q"
        queue.mkdir()
        config = tmp_path / "claude_runner.toml"
        config.write_text("", encoding="utf-8")

        mock_popen = self._popen_capturing_argv(999)
        with (
            patch.object(watchdog_cmd.shutil, "which", return_value="/usr/bin/claude-task-runner"),
            patch.object(watchdog_cmd.subprocess, "Popen", mock_popen),
        ):
            pid = _spawn_supervisor(queue, config)
        assert pid == 999
        argv = mock_popen.call_args.args[0]
        assert argv[:3] == ["/usr/bin/claude-task-runner", "supervisor", "start"]
        assert "--queue" in argv
        assert argv[argv.index("--queue") + 1] == str(queue)
        # The load-bearing assertion: --config is present and points at
        # the path the caller provided.
        assert "--config" in argv
        assert argv[argv.index("--config") + 1] == str(config)

    def test_spawn_omits_config_flag_when_none(self, tmp_path: Path) -> None:
        """No config path → no ``--config`` token (so the supervisor uses
        its own default-resolution, not an empty/garbage path)."""
        queue = tmp_path / "q"
        queue.mkdir()

        mock_popen = self._popen_capturing_argv(7)
        with (
            patch.object(watchdog_cmd.shutil, "which", return_value="/usr/bin/claude-task-runner"),
            patch.object(watchdog_cmd.subprocess, "Popen", mock_popen),
        ):
            pid = _spawn_supervisor(queue, None)
        assert pid == 7
        argv = mock_popen.call_args.args[0]
        assert "--config" not in argv


class TestCorruptRegistryBackup:
    """Audit finding 2: a corrupt registry must be logged + backed up,
    not silently reset to empty."""

    def test_corrupt_json_logs_and_backs_up(
        self,
        isolated_home: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        path = queues_registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")

        with caplog.at_level("ERROR", logger="claude_task_runner.cli.watchdog_cmd"):
            out = load_registered_queues()

        assert out == []
        # A .broken backup must be written alongside the original.
        backup = path.with_suffix(path.suffix + ".broken")
        assert backup.exists()
        assert backup.read_text(encoding="utf-8") == "{not json"
        # And the failure must be logged at ERROR with the path.
        assert any(
            record.levelname == "ERROR" and str(path) in record.getMessage()
            for record in caplog.records
        ), caplog.text

    def test_non_object_payload_logs_and_backs_up(
        self,
        isolated_home: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A syntactically-valid JSON that isn't an object (e.g. a list)
        is also corruption — same treatment."""
        path = queues_registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('["not", "a", "dict"]', encoding="utf-8")

        with caplog.at_level("ERROR", logger="claude_task_runner.cli.watchdog_cmd"):
            out = load_registered_queues()

        assert out == []
        backup = path.with_suffix(path.suffix + ".broken")
        assert backup.exists()
        assert any(record.levelname == "ERROR" for record in caplog.records), caplog.text

    def test_valid_registry_not_backed_up(self, isolated_home: Path) -> None:
        """A well-formed registry must NOT trigger a .broken backup."""
        queue = isolated_home / "q"
        queue.mkdir()
        register_queue(queue)
        backup = queues_registry_path().with_suffix(queues_registry_path().suffix + ".broken")
        assert load_registered_queues() == [queue.resolve()]
        assert not backup.exists()
