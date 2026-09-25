"""Tests for cli.watchdog_cmd — its subcommands and the tick's decision wiring.

The registry the tick walks is tested in ``test_cron_registry.py``."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from claude_task_runner.cli import watchdog_cmd
from claude_task_runner.cli.install_cmd import _watchdog_script_path
from claude_task_runner.cli.watchdog_cmd import _spawn_supervisor, app
from claude_task_runner.cron.backoff import WatchdogState, watchdog_state_path, write_state_atomic
from claude_task_runner.cron.registry import (
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


class TestRegisterCommand:
    def test_register_via_cli(self, runner: CliRunner, isolated_home: Path) -> None:
        queue = isolated_home / "q"
        queue.mkdir()
        result = runner.invoke(app, ["register", "--queue", str(queue)])
        assert result.exit_code == 0
        assert str(queue) in result.stdout
        assert load_registered_queues() == [queue.resolve()]

    def test_register_missing_directory_fails_loudly(
        self, runner: CliRunner, isolated_home: Path
    ) -> None:
        missing = isolated_home / "no-such-queue"
        result = runner.invoke(app, ["register", "--queue", str(missing)])
        assert result.exit_code == 2
        expected = f"register failed: not an existing directory: {missing.resolve()}"
        assert expected in result.stderr
        assert "registered:" not in result.stdout
        assert not queues_registry_path().exists()


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


class TestTickSettingsSource:
    """Where a tick's ``[watchdog]`` settings come from.

    These pin the current behaviour: the crontab's tick loads no queue's
    ``claude_runner.toml``, so a queue's ``[watchdog]`` table never takes
    effect."""

    def test_tick_ignores_the_queue_toml_watchdog_table(
        self, runner: CliRunner, isolated_home: Path
    ) -> None:
        """A 999 s cooldown in the queue's TOML does not stop a restart
        60 s after the last one; the package default of 30 s decides."""
        queue = isolated_home / "q"
        (queue / ".claude_task_runner").mkdir(parents=True)
        (queue / "claude_runner.toml").write_text(
            "[watchdog]\nrestart_cooldown_s = 999\n", encoding="utf-8"
        )
        register_queue(queue)
        last_restart = datetime.now(UTC) - timedelta(seconds=60)
        write_state_atomic(WatchdogState(recent_restarts=[last_restart]), watchdog_state_path())

        result = runner.invoke(app, ["tick", "--dry-run"])

        assert result.exit_code == 0, result.output
        assert (
            f"watchdog queue={queue.resolve()} alive=False pid=None verdict=restart "
            "detail='restart approved (recent count: 2 of threshold 5)'\n"
        ) in result.stdout

    def test_watchdog_sh_runs_tick_with_no_config(self, isolated_home: Path) -> None:
        """The script the crontab line runs passes the tick no ``--config``.

        Runs the packaged ``watchdog.sh`` with a stand-in
        ``claude-task-runner`` first on its PATH that prints its argv,
        which the script appends to ``watchdog.log``."""
        bin_dir = isolated_home / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        stand_in = bin_dir / "claude-task-runner"
        stand_in.write_text('#!/usr/bin/env bash\necho "argv: $*"\n', encoding="utf-8")
        stand_in.chmod(0o755)

        proc = subprocess.run(
            ["bash", str(_watchdog_script_path())],
            env={"HOME": str(isolated_home), "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        assert proc.returncode == 0, proc.stderr
        log = isolated_home / ".claude_task_runner" / "watchdog.log"
        assert log.read_text(encoding="utf-8") == "argv: watchdog tick\n"


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
