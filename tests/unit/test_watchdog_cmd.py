"""Tests for cli.watchdog_cmd — its subcommands and the tick's decision wiring.

The registry the tick walks is tested in ``test_cron_registry.py``."""

from __future__ import annotations

import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from claude_task_runner.cli import watchdog_cmd
from claude_task_runner.cli.install_cmd import _watchdog_script_path
from claude_task_runner.cli.watchdog_cmd import _spawn_supervisor, app
from claude_task_runner.cron.backoff import (
    WatchdogState,
    load_state,
    watchdog_state_path,
    write_state_atomic,
)
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


class TestUnregisterCommand:
    def test_unregister_a_deleted_queue(self, runner: CliRunner, isolated_home: Path) -> None:
        queue = isolated_home / "q"
        keep = isolated_home / "keep"
        queue.mkdir()
        keep.mkdir()
        register_queue(queue)
        register_queue(keep)
        queue.rmdir()

        result = runner.invoke(app, ["unregister", "--queue", str(queue)])

        assert result.exit_code == 0, result.output
        assert result.stdout == f"unregistered: {queue.resolve()}\n"
        assert result.stderr == ""
        assert load_registered_queues() == [keep.resolve()]
        assert not queue.exists()

    def test_unregister_defaults_to_the_current_directory(
        self, runner: CliRunner, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        queue = isolated_home / "q"
        queue.mkdir()
        register_queue(queue)
        monkeypatch.chdir(queue)
        result = runner.invoke(app, ["unregister"])
        assert result.exit_code == 0, result.output
        assert result.stdout == f"unregistered: {queue.resolve()}\n"
        assert load_registered_queues() == []

    def test_unregister_is_idempotent(self, runner: CliRunner, isolated_home: Path) -> None:
        queue = isolated_home / "q"
        queue.mkdir()
        register_queue(queue)
        assert runner.invoke(app, ["unregister", "--queue", str(queue)]).exit_code == 0

        result = runner.invoke(app, ["unregister", "--queue", str(queue)])

        assert result.exit_code == 0, result.output
        assert result.stdout == f"not registered: {queue.resolve()}\n"
        assert load_registered_queues() == []

    def test_unregister_with_no_registry(self, runner: CliRunner, isolated_home: Path) -> None:
        missing = isolated_home / "never-registered"
        result = runner.invoke(app, ["unregister", "--queue", str(missing)])
        assert result.exit_code == 0, result.output
        assert result.stdout == f"not registered: {missing.resolve()}\n"
        assert not queues_registry_path().exists()

    def test_unregister_corrupt_registry_fails_loudly(
        self, runner: CliRunner, isolated_home: Path
    ) -> None:
        registry = queues_registry_path()
        registry.parent.mkdir(parents=True)
        registry.write_text("{not json", encoding="utf-8")

        result = runner.invoke(app, ["unregister", "--queue", str(isolated_home / "q")])

        assert result.exit_code == 2
        assert result.stdout == ""
        assert result.stderr.startswith(
            f"unregister failed: corrupt queues registry at {registry} ("
        )
        assert registry.read_text(encoding="utf-8") == "{not json"
        assert sorted(p.name for p in registry.parent.iterdir()) == ["queues.json"]

    def test_unregister_write_failure_fails_loudly(
        self, runner: CliRunner, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        queue = isolated_home / "q"
        queue.mkdir()
        register_queue(queue)

        def _deny(_src: object, _dst: object) -> None:
            raise PermissionError(13, "Permission denied", str(queues_registry_path()))

        monkeypatch.setattr(watchdog_cmd.registry_mod.os, "replace", _deny)
        result = runner.invoke(app, ["unregister", "--queue", str(queue)])

        assert result.exit_code == 2
        assert result.stderr == (
            f"unregister failed: [Errno 13] Permission denied: '{queues_registry_path()}'\n"
        )
        assert "unregistered:" not in result.stdout
        assert load_registered_queues() == [queue.resolve()]
        # The temporary file the failed rename left is cleaned up.
        assert sorted(p.name for p in queues_registry_path().parent.iterdir()) == ["queues.json"]


class TestQueuesCommand:
    def test_queues_lists_registered(self, runner: CliRunner, isolated_home: Path) -> None:
        for name in ("a", "b"):
            (isolated_home / name).mkdir()
            register_queue(isolated_home / name)
        result = runner.invoke(app, ["queues"])
        assert result.exit_code == 0
        assert "a" in result.stdout
        assert "b" in result.stdout

    def test_queues_warns_about_a_missing_queue_on_stderr(
        self, runner: CliRunner, isolated_home: Path
    ) -> None:
        """Stdout stays one path per line, so a script reading it is unaffected."""
        gone = isolated_home / "gone"
        live = isolated_home / "live"
        gone.mkdir()
        live.mkdir()
        register_queue(gone)
        register_queue(live)
        gone.rmdir()

        result = runner.invoke(app, ["queues"])

        assert result.exit_code == 0, result.output
        assert result.stdout == f"{gone.resolve()}\n{live.resolve()}\n"
        assert result.stderr == (
            f"warning: {gone.resolve()} is not an existing directory, so the watchdog "
            "skips it. To stop managing it, run: claude-task-runner watchdog unregister "
            f"--queue {gone.resolve()}\n"
        )


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


def _record_spawns(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Replace ``_spawn_supervisor`` with a recorder, so nothing real is spawned."""
    spawned: list[Path] = []

    def _record(queue_dir: Path, config: Path | None = None) -> int:
        spawned.append(queue_dir)
        return 4242

    monkeypatch.setattr(watchdog_cmd, "_spawn_supervisor", _record)
    return spawned


def _skipped_line(queue: Path) -> str:
    """The ERROR line a tick writes to watchdog.log for a queue it skips, after the timestamp."""
    return (
        f" watchdog: ERROR queue={queue} is not an existing directory, so its "
        "supervisor was not restarted and the directory was not created. If the "
        "queue moved, register its new path. If it is gone for good, run: "
        f"claude-task-runner watchdog unregister --queue {queue}\n"
    )


class TestTickSkipsMissingQueue:
    """A registered queue whose directory was later deleted, moved or replaced.

    ``register_queue`` checks the path only when it registers it. The tick
    used to approve a restart anyway, and ``_spawn_supervisor`` recreated the
    directory with ``parents=True``. The supervisor started on that empty
    queue held the per-user global lock, so the real queue's supervisor
    failed with "another supervisor is already running"."""

    def test_deleted_queue_is_not_restarted_or_recreated(
        self,
        runner: CliRunner,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        queue = isolated_home / "q"
        queue.mkdir()
        register_queue(queue)
        queue.rmdir()
        spawned = _record_spawns(monkeypatch)

        result = runner.invoke(app, ["tick"])

        assert result.exit_code == 0, result.output
        assert spawned == []
        assert not queue.exists()
        assert result.stdout.count(_skipped_line(queue.resolve())) == 1
        assert "verdict=" not in result.stdout
        # The skip takes no slot in the restart history that every queue shares.
        assert load_state(watchdog_state_path()).recent_restarts == []
        # The entry stays: a queue on an unmounted filesystem comes back by itself.
        assert load_registered_queues() == [queue.resolve()]

    def test_path_replaced_by_a_file_is_skipped(
        self,
        runner: CliRunner,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        queue = isolated_home / "q"
        queue.mkdir()
        register_queue(queue)
        queue.rmdir()
        queue.write_text("not a queue\n", encoding="utf-8")
        spawned = _record_spawns(monkeypatch)

        result = runner.invoke(app, ["tick"])

        assert result.exit_code == 0, result.output
        assert spawned == []
        assert queue.read_text(encoding="utf-8") == "not a queue\n"
        assert result.stdout.count(_skipped_line(queue.resolve())) == 1

    def test_missing_queue_does_not_starve_a_live_one(
        self,
        runner: CliRunner,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Both supervisors are down, and the missing queue is listed first.

        Every queue shares one restart cooldown. The missing queue used to take
        the restart, which left the real queue in cooldown on every tick."""
        gone = isolated_home / "gone"
        real = isolated_home / "real"
        gone.mkdir()
        real.mkdir()
        register_queue(gone)
        register_queue(real)
        gone.rmdir()
        spawned = _record_spawns(monkeypatch)

        result = runner.invoke(app, ["tick"])

        assert result.exit_code == 0, result.output
        assert spawned == [real.resolve()]
        assert result.stdout.count(_skipped_line(gone.resolve())) == 1
        assert f"watchdog queue={real.resolve()} alive=False pid=None verdict=restart" in (
            result.stdout
        )
        assert len(load_state(watchdog_state_path()).recent_restarts) == 1

    @pytest.mark.skipif(os.geteuid() == 0, reason="root is not denied by directory permissions")
    def test_queue_behind_an_unsearchable_directory_does_not_end_the_tick(
        self,
        runner: CliRunner,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Path.is_dir raises PermissionError here on Python 3.12 and 3.13.

        Raised in the loop, it would end the tick before the queues after it."""
        locked = isolated_home / "locked"
        hidden = locked / "q"
        real = isolated_home / "real"
        hidden.mkdir(parents=True)
        real.mkdir()
        register_queue(hidden)
        register_queue(real)
        spawned = _record_spawns(monkeypatch)
        locked.chmod(0o000)
        try:
            result = runner.invoke(app, ["tick"])
        finally:
            locked.chmod(0o700)

        assert result.exit_code == 0, result.output
        assert spawned == [real.resolve()]
        assert result.stdout.count(_skipped_line(hidden.resolve())) == 1

    def test_dry_run_reports_the_missing_queue(
        self,
        runner: CliRunner,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        queue = isolated_home / "q"
        queue.mkdir()
        register_queue(queue)
        queue.rmdir()
        spawned = _record_spawns(monkeypatch)

        result = runner.invoke(app, ["tick", "--dry-run"])

        assert result.exit_code == 0, result.output
        assert spawned == []
        assert result.stdout.count(_skipped_line(queue.resolve())) == 1
        assert "verdict=" not in result.stdout

    def test_queue_deleted_after_the_check_is_not_recreated(
        self,
        runner: CliRunner,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The queue disappears between the tick's check and the spawn."""
        queue = isolated_home / "q"
        queue.mkdir()
        register_queue(queue)
        real_spawn = watchdog_cmd._spawn_supervisor

        def _delete_then_spawn(queue_dir: Path, config: Path | None = None) -> int:
            queue_dir.rmdir()
            return real_spawn(queue_dir, config)

        popen = MagicMock()
        monkeypatch.setattr(watchdog_cmd, "_spawn_supervisor", _delete_then_spawn)
        monkeypatch.setattr(watchdog_cmd.subprocess, "Popen", popen)
        with patch.object(watchdog_cmd.shutil, "which", return_value="/usr/bin/claude-task-runner"):
            result = runner.invoke(app, ["tick"])

        assert result.exit_code == 0, result.output
        assert not queue.exists()
        popen.assert_not_called()
        log_dir = queue.resolve() / ".claude_task_runner"
        assert (
            f" watchdog: spawn failed for {queue.resolve()}: "
            f"[Errno 2] No such file or directory: '{log_dir}'\n"
        ) in result.stdout
        # A failed restart still counts toward crash-loop backoff, as before.
        assert len(load_state(watchdog_state_path()).recent_restarts) == 1


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

    def test_spawn_does_not_recreate_a_missing_queue(self, tmp_path: Path) -> None:
        """The last guard if the queue disappears after the tick checked it.

        It used to make ``<queue>/.claude_task_runner`` with ``parents=True``,
        which recreated the deleted queue before starting a supervisor on it."""
        queue = tmp_path / "gone"

        mock_popen = self._popen_capturing_argv(7)
        with (
            patch.object(watchdog_cmd.shutil, "which", return_value="/usr/bin/claude-task-runner"),
            patch.object(watchdog_cmd.subprocess, "Popen", mock_popen),
            pytest.raises(FileNotFoundError, match=re.escape(str(queue / ".claude_task_runner"))),
        ):
            _spawn_supervisor(queue, None)
        assert not queue.exists()
        mock_popen.assert_not_called()

    def test_spawn_without_the_cli_on_path(self, tmp_path: Path) -> None:
        queue = tmp_path / "q"
        queue.mkdir()
        with (
            patch.object(watchdog_cmd.shutil, "which", return_value=None),
            pytest.raises(RuntimeError, match=r"^claude-task-runner not on PATH$"),
        ):
            _spawn_supervisor(queue, None)
        assert list(queue.iterdir()) == []

    def test_spawn_creates_the_log_dir_of_a_fresh_queue(self, tmp_path: Path) -> None:
        """A queue that has never run has no ``.claude_task_runner/`` yet."""
        queue = tmp_path / "q"
        queue.mkdir()

        mock_popen = self._popen_capturing_argv(7)
        with (
            patch.object(watchdog_cmd.shutil, "which", return_value="/usr/bin/claude-task-runner"),
            patch.object(watchdog_cmd.subprocess, "Popen", mock_popen),
        ):
            assert _spawn_supervisor(queue, None) == 7
        assert (queue / ".claude_task_runner" / "supervisor.log").is_file()
