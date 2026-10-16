"""A configuration error stops ``supervisor start`` (ADR-0035, 2026-10-16 amendment).

Pins: ``supervisor start`` refuses to start, before it touches the queue,
when any account's ``runner-account.toml`` does not load or the queue TOML
does not; it lists every broken account and exits 78 (``EX_CONFIG``), which
the systemd unit does not restart on. A ``config_dir`` whose ``~user`` names
no user fails the queue TOML at load, and a SIGHUP reload carrying one is
rejected with an ERROR. The cron watchdog holds back a restart for the same
reasons. While the supervisor runs, a file that breaks stays non-fatal
(``test_broken_account_file.py``), and a user removed mid-run fails only
that account at every place that expands its ``config_dir``.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import pwd
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from claude_task_runner.cli import app, watchdog_cmd
from claude_task_runner.config.loader import (
    CONFIG_ERROR_EXIT_STATUS,
    PER_ACCOUNT_TOML_NAME,
    BrokenAccount,
    ConfigError,
    load_account_policy,
    load_settings,
    os_error_is_transient,
    resolve_accounts,
)
from claude_task_runner.config.paths import ConfigDirError, expand_config_dir
from claude_task_runner.config.schema import AccountSettings
from claude_task_runner.cron import backoff as backoff_mod
from claude_task_runner.cron.registry import register_queue
from claude_task_runner.supervisor.daemon import _apply_sighup_reload

from ._concurrency_fixtures import (
    DAEMON_LOGGER,
    run_daemon,
    sighup_in_second_tick,
    single_account_queue,
    write_account_cap,
)

SUPERVISOR_CMD_LOGGER = "claude_task_runner.cli.supervisor_cmd"

BAD_TOML = "max_concurrency 5\n"
PARSE_ERROR = "Expected '=' after a key in a key/value pair (at line 1, column 17)"
GONE = "~no-such-user-ctr-test/.claude"
"""A ``config_dir`` whose ``~user`` names no user."""

REFUSAL_TAIL = (
    "Fix each one (a runner-account.toml can also be deleted for the defaults), then start "
    "the supervisor again. Once the supervisor runs, a problem like this only stops new "
    "tasks on its account."
)


def _queue(tmp_path: Path, accounts: dict[str, str]) -> Path:
    """A queue directory whose ``claude_runner.toml`` declares ``accounts``."""
    queue = tmp_path / "q"
    queue.mkdir()
    body = "".join(
        f'[[accounts]]\nname = "{name}"\nconfig_dir = "{config_dir}"\n\n'
        for name, config_dir in accounts.items()
    )
    (queue / "claude_runner.toml").write_text(body, encoding="utf-8")
    return queue


def _accounts(tmp_path: Path) -> dict[str, str]:
    """``personal`` (cap 5) and ``work`` (cap 3), each with a file that loads."""
    dirs = {"personal": tmp_path / "personal", "work": tmp_path / "work"}
    write_account_cap(dirs["personal"], 5)
    write_account_cap(dirs["work"], 3)
    return {name: str(path) for name, path in dirs.items()}


def _start(queue: Path) -> object:
    """``supervisor start`` with the daemon and adoption made to fail if reached."""
    with (
        patch("claude_task_runner.cli.supervisor_cmd.configure_logging"),
        patch(
            "claude_task_runner.cli.supervisor_cmd.start_daemon",
            side_effect=AssertionError("start_daemon reached"),
        ),
        patch(
            "claude_task_runner.supervisor.adoption.adopt_running_workers",
            side_effect=AssertionError("adoption reached"),
        ),
    ):
        return CliRunner().invoke(app, ["supervisor", "start", "--queue", str(queue)])


# ---------------------------------------------------------------------------
# supervisor start refuses a broken account file
# ---------------------------------------------------------------------------


class TestStartRefuses:
    def test_one_broken_file(self, tmp_path: Path) -> None:
        accounts = _accounts(tmp_path)
        path = Path(accounts["personal"]) / PER_ACCOUNT_TOML_NAME
        path.write_text(BAD_TOML, encoding="utf-8")
        queue = _queue(tmp_path, accounts)
        result = _start(queue)
        assert result.exit_code == CONFIG_ERROR_EXIT_STATUS == 78
        assert result.stdout == (
            "supervisor not started: 1 account cannot take tasks:\n"
            f"  personal: Invalid TOML in {path}: {PARSE_ERROR}\n"
            f"{REFUSAL_TAIL}\n"
        )
        # Nothing on disk: no runtime directory, so no supervisor.json or PID file.
        assert not (queue / ".claude_task_runner").exists()

    def test_every_broken_file_is_listed(self, tmp_path: Path) -> None:
        accounts = _accounts(tmp_path)
        personal = Path(accounts["personal"]) / PER_ACCOUNT_TOML_NAME
        work = Path(accounts["work"]) / PER_ACCOUNT_TOML_NAME
        personal.write_text(BAD_TOML, encoding="utf-8")
        work.write_bytes(b"")
        queue = _queue(tmp_path, accounts)
        result = _start(queue)
        assert result.exit_code == 78
        assert result.stdout == (
            "supervisor not started: 2 accounts cannot take tasks:\n"
            f"  personal: Invalid TOML in {personal}: {PARSE_ERROR}\n"
            f"  work: {work} is empty; delete it for the defaults, or write the settings "
            "it should hold\n"
            f"{REFUSAL_TAIL}\n"
        )
        assert not (queue / ".claude_task_runner").exists()

    def test_a_linux_user_that_needs_missing_sudo(self, tmp_path: Path) -> None:
        """No worker could start for the account, so the start is refused as
        for a broken file; installing sudo, or removing linux_user, fixes it."""
        accounts = _accounts(tmp_path)
        queue = _queue(tmp_path, accounts)
        text = (queue / "claude_runner.toml").read_text(encoding="utf-8")
        (queue / "claude_runner.toml").write_text(
            text.replace('name = "work"\n', 'name = "work"\nlinux_user = "bill-work"\n'),
            encoding="utf-8",
        )
        with (
            patch("claude_task_runner.config.linux_user.current_username", return_value="bill"),
            patch("claude_task_runner.config.linux_user.shutil.which", return_value=None),
        ):
            result = _start(queue)
        assert result.exit_code == 78
        assert result.stdout == (
            "supervisor not started: 1 account cannot take tasks:\n"
            "  work: linux_user='bill-work' requested but sudo not on PATH\n"
            f"{REFUSAL_TAIL}\n"
        )
        assert not (queue / ".claude_task_runner").exists()

    def test_every_file_loading_starts_the_daemon(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path, _accounts(tmp_path))
        with (
            patch("claude_task_runner.cli.supervisor_cmd.configure_logging"),
            patch("claude_task_runner.cli.supervisor_cmd.start_daemon") as daemon,
        ):
            result = CliRunner().invoke(app, ["supervisor", "start", "--queue", str(queue)])
        assert result.exit_code == 0, result.output
        daemon.assert_called_once()

    def test_a_queue_toml_that_does_not_load_exits_78_too(self, tmp_path: Path) -> None:
        queue = tmp_path / "q"
        queue.mkdir()
        (queue / "claude_runner.toml").write_text("this is not [valid toml", encoding="utf-8")
        result = _start(queue)
        assert result.exit_code == 78
        assert result.stdout.startswith(
            f"supervisor not started: Invalid TOML in {queue / 'claude_runner.toml'}: "
        )
        assert not (queue / ".claude_task_runner").exists()


# ---------------------------------------------------------------------------
# A config_dir whose ~user names no user
# ---------------------------------------------------------------------------


def _unknown_user_error(account: str) -> str:
    """How the queue TOML's load error starts; pydantic's own tail (the
    input it echoes, its docs URL) follows."""
    return (
        "Settings validation failed: 1 validation error for Settings\n"
        "accounts.0\n"
        f"  Value error, account '{account}': config_dir '{GONE}' cannot be expanded: "
        "Could not determine home directory. [type=value_error, "
    )


UNKNOWN_USER_ERROR = _unknown_user_error("personal")


class TestUnknownHome:
    def test_the_queue_toml_does_not_load(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path, {"personal": GONE})
        with pytest.raises(ConfigError) as excinfo:
            load_settings(queue / "claude_runner.toml")
        assert str(excinfo.value).startswith(UNKNOWN_USER_ERROR)

    def test_the_legacy_config_dir_does_not_load_either(self, tmp_path: Path) -> None:
        toml = tmp_path / "claude_runner.toml"
        toml.write_text(f'[claude]\nconfig_dir = "{GONE}"\n', encoding="utf-8")
        with pytest.raises(ConfigError) as excinfo:
            load_settings(toml)
        assert f"account 'default': config_dir '{GONE}' cannot be expanded" in str(excinfo.value)

    def test_supervisor_start_refuses_it(self, tmp_path: Path) -> None:
        queue = _queue(tmp_path, {"personal": GONE})
        result = _start(queue)
        assert result.exit_code == 78
        assert result.stdout.startswith(f"supervisor not started: {UNKNOWN_USER_ERROR}")
        assert not (queue / ".claude_task_runner").exists()

    def test_a_sighup_reload_carrying_it_is_rejected(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        current = load_settings(None)
        queue = _queue(tmp_path, {"personal": GONE})
        with caplog.at_level(logging.INFO, logger=DAEMON_LOGGER):
            new_settings, pending = _apply_sighup_reload(
                current=current,
                config_path=queue / "claude_runner.toml",
                prior_pending=4,
                pending_count_fn=lambda: 99,
            )
        assert new_settings is current
        assert pending == 4
        (record,) = [r for r in caplog.records if r.name == DAEMON_LOGGER]
        assert record.levelno == logging.ERROR
        assert record.getMessage().startswith(
            f"SIGHUP received: reload failed ({UNKNOWN_USER_ERROR}"
        )
        assert record.getMessage().endswith("); keeping old config")

    @pytest.mark.allow_self_signal
    @pytest.mark.usefixtures("private_global_lock")
    def test_a_running_supervisor_keeps_its_config(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        qd, config = single_account_queue(tmp_path, "")
        good = config.read_text(encoding="utf-8")

        def gone_user() -> None:
            config.write_text(good.replace(str(tmp_path / "acct"), GONE), encoding="utf-8")

        with caplog.at_level(logging.INFO, logger=DAEMON_LOGGER):
            run_daemon(
                qd,
                config,
                pending_count_fn=sighup_in_second_tick(gone_user),
                install_signal_handlers=True,
            )
        errors = [
            r.getMessage()
            for r in caplog.records
            if r.name == DAEMON_LOGGER and r.levelno == logging.ERROR
        ]
        assert len(errors) == 1
        assert errors[0].startswith(
            "SIGHUP received: reload failed (Settings validation failed: 2 validation errors "
            "for Settings\nclaude.config_dir\n  Value error, [claude].config_dir "
            f"'{GONE}' cannot be expanded: Could not determine home directory. "
        )
        assert f"account 'default': config_dir '{GONE}' cannot be expanded" in errors[0]
        # The old config stays, so the ceiling is not announced again.
        assert [
            r.getMessage()
            for r in caplog.records
            if r.getMessage().startswith("concurrency ceiling: ")
        ] == ["concurrency ceiling: 3, the sum of the accounts' max_concurrency (default 3)"]


class TestUserRemovedWhileRunning:
    """Every place that expands a ``config_dir`` fails only that account."""

    def test_the_helper_names_the_config_dir(self) -> None:
        with pytest.raises(ConfigDirError) as excinfo:
            expand_config_dir(GONE)
        assert str(excinfo.value) == (
            f"config_dir '{GONE}' cannot be expanded: Could not determine home directory."
        )

    def test_usage_capture_reports_a_spawn_error(self) -> None:
        from claude_task_runner.usage.capture import _build_spawn_env
        from claude_task_runner.usage.drift import UsageCaptureSpawnError

        with pytest.raises(UsageCaptureSpawnError) as excinfo:
            _build_spawn_env(GONE)
        assert str(excinfo.value) == (
            f"config_dir '{GONE}' cannot be expanded: Could not determine home directory."
        )

    def test_the_api_source_reports_missing_credentials(self) -> None:
        from claude_task_runner.usage.api_source import CredentialsNotFound, _read_oauth_token

        with pytest.raises(CredentialsNotFound) as excinfo:
            _read_oauth_token(GONE)
        assert str(excinfo.value) == (
            f"config_dir '{GONE}' cannot be expanded: Could not determine home directory."
        )

    @pytest.mark.parametrize("mode", ["tty", "api", "api_then_tty"])
    def test_the_supervisor_still_builds_its_usage_sources(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, mode: str
    ) -> None:
        """``supervisor start`` builds one usage source per account before the
        daemon runs; a removed user warns and leaves that account's captures
        to fail, instead of ending the start with a traceback."""
        from claude_task_runner.cli.supervisor_cmd import _build_usage_source
        from claude_task_runner.config.schema import AccountSettings
        from claude_task_runner.usage.multi_account_source import MultiAccountUsageSource

        loaded = load_settings(None)
        settings = loaded.model_copy(
            update={
                "usage": loaded.usage.model_copy(update={"source": mode}),
                # model_construct: the validator refuses this config_dir at load.
                "accounts": [
                    AccountSettings(name="personal", config_dir=str(tmp_path / "personal")),
                    AccountSettings.model_construct(name="work", config_dir=GONE),
                ],
            }
        )
        with caplog.at_level(logging.WARNING, logger=SUPERVISOR_CMD_LOGGER):
            source = _build_usage_source(settings, tmp_path, lambda: None)
        assert isinstance(source, MultiAccountUsageSource)
        assert sorted(source._per_account) == ["personal", "work"]
        assert [
            (r.levelno, r.getMessage()) for r in caplog.records if r.name == SUPERVISOR_CMD_LOGGER
        ] == [
            (
                logging.WARNING,
                f"cannot check {GONE} for a long-lived token: config_dir '{GONE}' cannot "
                "be expanded: Could not determine home directory.",
            )
        ]


# ---------------------------------------------------------------------------
# The cron watchdog holds back a restart that would be refused
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


class TestWatchdog:
    def test_a_down_supervisor_is_not_restarted(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        accounts = _accounts(isolated_home)
        path = Path(accounts["work"]) / PER_ACCOUNT_TOML_NAME
        path.write_text(BAD_TOML, encoding="utf-8")
        queue = _queue(isolated_home, accounts)
        (queue / ".claude_task_runner").mkdir()
        register_queue(queue)
        spawned: list[Path] = []
        monkeypatch.setattr(
            watchdog_cmd, "_spawn_supervisor", lambda qd, config=None: spawned.append(qd) or 1
        )
        result = CliRunner().invoke(app, ["watchdog", "tick"])
        assert result.exit_code == 1
        assert spawned == []
        # The held-back line replaces the verdict line, which would say "restart".
        (line,) = [ln for ln in result.stdout.splitlines() if str(queue) in ln]
        assert line.split(" ", 1)[1] == (
            f"watchdog: ERROR queue={queue} alive=False pid=None "
            "1 account(s) cannot take tasks, so its supervisor was not restarted: work: "
            f"Invalid TOML in {path}: {PARSE_ERROR}"
        )
        # No restart was counted, so the next tick after the fix restarts at once.
        state = backoff_mod.load_state(backoff_mod.watchdog_state_path())
        assert state.recent_restarts == []

    def test_a_running_supervisor_is_left_alone(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch, live_worker_pid: int
    ) -> None:
        accounts = _accounts(isolated_home)
        (Path(accounts["work"]) / PER_ACCOUNT_TOML_NAME).write_text(BAD_TOML, encoding="utf-8")
        queue = _queue(isolated_home, accounts)
        (queue / ".claude_task_runner").mkdir()
        (queue / ".claude_task_runner" / "supervisor.pid").write_text(f"{live_worker_pid}\n")
        register_queue(queue)
        result = CliRunner().invoke(app, ["watchdog", "tick"])
        assert result.exit_code == 0, result.output
        assert "verdict=skip" in result.stdout
        assert "ERROR" not in result.stdout

    def test_a_dry_run_reports_it_too(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        accounts = _accounts(isolated_home)
        path = Path(accounts["work"]) / PER_ACCOUNT_TOML_NAME
        path.write_text(BAD_TOML, encoding="utf-8")
        queue = _queue(isolated_home, accounts)
        (queue / ".claude_task_runner").mkdir()
        register_queue(queue)
        monkeypatch.setattr(watchdog_cmd, "_spawn_supervisor", _no_spawn)
        result = CliRunner().invoke(app, ["watchdog", "tick", "--dry-run"])
        assert result.exit_code == 1
        assert "verdict=restart" not in result.stdout
        assert (
            f"watchdog: ERROR queue={queue} alive=False pid=None 1 account(s) cannot take tasks"
        ) in result.stdout
        assert not backoff_mod.watchdog_state_path().exists()

    def test_a_cooldown_is_left_alone(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The files are read only when a restart is due; a tick in cooldown
        logs its verdict as before."""
        accounts = _accounts(isolated_home)
        (Path(accounts["work"]) / PER_ACCOUNT_TOML_NAME).write_text(BAD_TOML, encoding="utf-8")
        queue = _queue(isolated_home, accounts)
        (queue / ".claude_task_runner").mkdir()
        register_queue(queue)
        backoff_mod.write_state_atomic(
            backoff_mod.WatchdogState(
                queue=queue.resolve(), recent_restarts=[datetime.now(UTC) - timedelta(seconds=5)]
            ),
            backoff_mod.watchdog_state_path(),
        )
        monkeypatch.setattr(watchdog_cmd, "_spawn_supervisor", _no_spawn)
        result = CliRunner().invoke(app, ["watchdog", "tick"])
        assert result.exit_code == 0, result.output
        assert "verdict=cooldown" in result.stdout
        assert "runner-account.toml" not in result.stdout

    def test_a_file_that_may_come_back_is_a_warning(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        accounts = _accounts(isolated_home)
        unmounted = isolated_home / "unmounted"
        accounts["work"] = str(unmounted)
        queue = _queue(isolated_home, accounts)
        (queue / ".claude_task_runner").mkdir()
        register_queue(queue)
        monkeypatch.setattr(watchdog_cmd, "_spawn_supervisor", _no_spawn)
        before = datetime.now(UTC)
        result = CliRunner().invoke(app, ["watchdog", "tick"])
        assert result.exit_code == 0, result.output
        (line,) = [ln for ln in result.stdout.splitlines() if str(queue) in ln]
        assert line.split(" ", 1)[1] == (
            f"watchdog: WARNING queue={queue} alive=False pid=None "
            "1 account(s) cannot take tasks, so its supervisor was not restarted; this may "
            "clear up by itself, and the next tick tries again: work: config_dir "
            f"{unmounted} does not exist"
        )
        state = backoff_mod.load_state(backoff_mod.watchdog_state_path())
        assert state.recent_restarts == []
        assert state.transient_config_since is not None
        assert before <= state.transient_config_since <= datetime.now(UTC)

    def test_one_content_error_among_transient_ones_is_an_error(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only a fix helps the account whose file is wrong, so the tick fails
        at once, however recent the other account's problem is."""
        accounts = _accounts(isolated_home)
        personal = Path(accounts["personal"]) / PER_ACCOUNT_TOML_NAME
        personal.write_text(BAD_TOML, encoding="utf-8")
        unmounted = isolated_home / "unmounted"
        accounts["work"] = str(unmounted)
        queue = _queue(isolated_home, accounts)
        (queue / ".claude_task_runner").mkdir()
        register_queue(queue)
        monkeypatch.setattr(watchdog_cmd, "_spawn_supervisor", _no_spawn)
        result = CliRunner().invoke(app, ["watchdog", "tick"])
        assert result.exit_code == 1
        (line,) = [ln for ln in result.stdout.splitlines() if str(queue) in ln]
        assert line.split(" ", 1)[1] == (
            f"watchdog: ERROR queue={queue} alive=False pid=None "
            "2 account(s) cannot take tasks, so its supervisor was not restarted: personal: "
            f"Invalid TOML in {personal}: {PARSE_ERROR}; work: config_dir {unmounted} "
            "does not exist"
        )
        state = backoff_mod.load_state(backoff_mod.watchdog_state_path())
        assert state.transient_config_since is None

    @pytest.mark.parametrize(
        ("escalation_s", "level", "exit_code", "note"),
        [
            pytest.param(
                900,
                "WARNING",
                0,
                "this may clear up by itself, and the next tick tries again",
                id="within-the-queue-setting",
            ),
            pytest.param(
                600,
                "ERROR",
                1,
                "this may clear up by itself, but has lasted 12 min",
                id="past-the-queue-setting",
            ),
        ],
    )
    def test_a_file_that_stays_away_becomes_an_error(
        self,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        escalation_s: int,
        level: str,
        exit_code: int,
        note: str,
    ) -> None:
        """The queue's ``[watchdog].transient_config_escalation_s`` decides when a
        problem that may clear up has lasted too long; its start is kept."""
        accounts = _accounts(isolated_home)
        unmounted = isolated_home / "unmounted"
        accounts["work"] = str(unmounted)
        queue = _queue(isolated_home, accounts)
        with (queue / "claude_runner.toml").open("a", encoding="utf-8") as fh:
            fh.write(f"[watchdog]\ntransient_config_escalation_s = {escalation_s}\n")
        (queue / ".claude_task_runner").mkdir()
        register_queue(queue)
        since = datetime.now(UTC) - timedelta(minutes=12, seconds=5)
        backoff_mod.write_state_atomic(
            backoff_mod.WatchdogState(queue=queue.resolve(), transient_config_since=since),
            backoff_mod.watchdog_state_path(),
        )
        monkeypatch.setattr(watchdog_cmd, "_spawn_supervisor", _no_spawn)
        result = CliRunner().invoke(app, ["watchdog", "tick"])
        assert result.exit_code == exit_code, result.output
        (line,) = [ln for ln in result.stdout.splitlines() if str(queue) in ln]
        assert line.split(" ", 1)[1] == (
            f"watchdog: {level} queue={queue} alive=False pid=None "
            f"1 account(s) cannot take tasks, so its supervisor was not restarted; {note}: "
            f"work: config_dir {unmounted} does not exist"
        )
        state = backoff_mod.load_state(backoff_mod.watchdog_state_path())
        assert state.transient_config_since == since
        assert state.recent_restarts == []

    def test_a_restart_ends_a_transient_spell(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        queue = _queue(isolated_home, _accounts(isolated_home))
        (queue / ".claude_task_runner").mkdir()
        register_queue(queue)
        backoff_mod.write_state_atomic(
            backoff_mod.WatchdogState(
                queue=queue.resolve(),
                transient_config_since=datetime.now(UTC) - timedelta(minutes=30),
            ),
            backoff_mod.watchdog_state_path(),
        )
        spawned: list[Path] = []
        monkeypatch.setattr(
            watchdog_cmd, "_spawn_supervisor", lambda qd, config=None: spawned.append(qd) or 1
        )
        result = CliRunner().invoke(app, ["watchdog", "tick"])
        assert result.exit_code == 0, result.output
        assert spawned == [queue.resolve()]
        state = backoff_mod.load_state(backoff_mod.watchdog_state_path())
        assert state.transient_config_since is None


def _no_spawn(_queue_dir: Path, config: Path | None = None) -> int:
    raise AssertionError("a supervisor was spawned")


# ---------------------------------------------------------------------------
# Exit 78 when the content is wrong, 1 when a file may come back
# ---------------------------------------------------------------------------

NON_UTF8 = b"\xff\xfe"
UTF8_ERROR = "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte"


class TestLoadSettingsErrors:
    """``load_settings`` raises ConfigError for every way the file can fail."""

    def _error(self, path: Path) -> ConfigError:
        with pytest.raises(ConfigError) as excinfo:
            load_settings(path)
        return excinfo.value

    def test_not_utf8_is_content(self, tmp_path: Path) -> None:
        path = tmp_path / "claude_runner.toml"
        path.write_bytes(NON_UTF8)
        error = self._error(path)
        assert str(error) == f"Invalid TOML in {path}: {UTF8_ERROR}"
        assert error.transient is False

    def test_a_directory_is_content(self, tmp_path: Path) -> None:
        error = self._error(tmp_path)
        assert str(error) == f"Settings file is a directory: {tmp_path}"
        assert error.transient is False

    def test_a_missing_file_may_come_back(self, tmp_path: Path) -> None:
        error = self._error(tmp_path / "absent.toml")
        assert str(error) == f"Settings file not found: {tmp_path / 'absent.toml'}"
        assert error.transient is True

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
    def test_a_denied_file_may_come_back(self, tmp_path: Path) -> None:
        path = tmp_path / "claude_runner.toml"
        path.write_text("", encoding="utf-8")
        path.chmod(0)
        try:
            error = self._error(path)
        finally:
            path.chmod(0o644)
        assert str(error) == f"Cannot read {path}: [Errno 13] Permission denied: '{path}'"
        assert error.transient is True


# ---------------------------------------------------------------------------
# One errno classifier for both loaders
# ---------------------------------------------------------------------------

LOOP = "[Errno 40] Too many levels of symbolic links"
TOO_LONG = "[Errno 36] File name too long"
LONG_NAME = "x" * 300


@pytest.mark.parametrize(
    ("code", "transient"),
    [
        (errno.ELOOP, False),
        (errno.ENAMETOOLONG, False),
        (errno.ENOTDIR, False),
        (errno.EISDIR, False),
        (errno.ENXIO, False),
        (errno.EACCES, True),
        (errno.EPERM, True),
        (errno.ENOENT, True),
        (errno.EIO, True),
        (errno.ESTALE, True),
        (errno.EHOSTDOWN, True),
        (errno.ENOTCONN, True),
    ],
    ids=lambda v: errno.errorcode[v] if isinstance(v, int) and not isinstance(v, bool) else None,
)
def test_the_classifier(code: int, transient: bool) -> None:
    """A path that is wrong (a loop, a name too long, a file where a directory
    belongs, or the reverse) cannot be fixed by retrying; anything else may
    clear up: permissions get fixed, mounts come up."""
    assert os_error_is_transient(OSError(code, os.strerror(code))) is transient


def _queue_error(path: Path) -> ConfigError:
    with pytest.raises(ConfigError) as excinfo:
        load_settings(path)
    return excinfo.value


def _account_error(config_dir: Path | str) -> ConfigError:
    with pytest.raises(ConfigError) as excinfo:
        load_account_policy(str(config_dir))
    return excinfo.value


class TestQueueTomlErrno:
    def test_a_symlink_loop_is_content(self, tmp_path: Path) -> None:
        path = tmp_path / "claude_runner.toml"
        path.symlink_to(path)
        error = _queue_error(path)
        assert str(error) == f"Cannot read {path}: {LOOP}: '{path}'"
        assert error.transient is False

    def test_a_name_too_long_is_content(self, tmp_path: Path) -> None:
        path = tmp_path / f"{LONG_NAME}.toml"
        error = _queue_error(path)
        assert str(error) == f"Cannot read {path}: {TOO_LONG}: '{path}'"
        assert error.transient is False

    @pytest.mark.parametrize("depth", [0, 2], ids=["parent", "ancestor"])
    def test_a_path_through_a_file_is_content(self, tmp_path: Path, depth: int) -> None:
        """``--config /etc/passwd/x.toml``, say."""
        not_a_dir = tmp_path / "passwd"
        not_a_dir.write_text("", encoding="utf-8")
        path = not_a_dir.joinpath(*["sub"] * depth, "x.toml")
        error = _queue_error(path)
        assert str(error) == f"{not_a_dir} is not a directory, so {path} cannot be read"
        assert error.transient is False

    def test_a_symlink_to_nothing_may_come_back(self, tmp_path: Path) -> None:
        path = tmp_path / "claude_runner.toml"
        target = tmp_path / "unmounted" / "claude_runner.toml"
        path.symlink_to(target)
        error = _queue_error(path)
        assert str(error) == f"{path} is a symlink to {target}, which does not exist"
        assert error.transient is True

    @pytest.mark.parametrize("code", [errno.EIO, errno.ESTALE, errno.ENOTCONN, errno.EPERM])
    def test_an_injected_read_error_may_come_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
    ) -> None:
        path = tmp_path / "claude_runner.toml"
        path.write_text("", encoding="utf-8")
        _fail_open(monkeypatch, path, code)
        error = _queue_error(path)
        assert str(error) == f"Cannot read {path}: [Errno {code}] {os.strerror(code)}"
        assert error.transient is True

    def test_nesting_too_deep_is_content(self, tmp_path: Path) -> None:
        path = tmp_path / "claude_runner.toml"
        path.write_text(DEEP_TOML, encoding="utf-8")
        error = _queue_error(path)
        assert str(error) == f"{path} is nested too deeply to load"
        assert error.transient is False

    def test_a_nul_in_a_config_dir_is_content(self, tmp_path: Path) -> None:
        path = tmp_path / "claude_runner.toml"
        path.write_text(
            '[[accounts]]\nname = "personal"\nconfig_dir = "/tmp/x\\u0000y"\n',
            encoding="utf-8",
        )
        error = _queue_error(path)
        assert "account 'personal': config_dir '/tmp/x\\x00y' contains a NUL byte" in str(error)
        assert error.transient is False


def _bounded(call: Callable[[], object], timeout_s: float = 10.0) -> object:
    """``call()``, failing the test if it has not returned within ``timeout_s``.

    Runs it on a daemon thread, so a call blocked on a FIFO cannot keep the
    test process alive."""
    outcome: dict[str, object] = {}

    def run() -> None:
        try:
            outcome["value"] = call()
        except BaseException as exc:  # handed back to the test thread
            outcome["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout_s)
    assert not thread.is_alive(), f"still blocked after {timeout_s} s"
    if "error" in outcome:
        raise outcome["error"]  # type: ignore[misc]
    return outcome["value"]


class TestNotARegularFile:
    """A FIFO, a device or a socket where a configuration file belongs is a
    content error, found before the file is read: a FIFO with no writer
    would otherwise block the reader, the supervisor's tick included."""

    def test_a_fifo_as_the_queue_toml(self, tmp_path: Path) -> None:
        fifo = tmp_path / "claude_runner.toml"
        os.mkfifo(fifo)
        with pytest.raises(ConfigError) as excinfo:
            _bounded(lambda: load_settings(fifo))
        assert str(excinfo.value) == f"Settings file is a FIFO, not a regular file: {fifo}"
        assert excinfo.value.transient is False

    def test_a_queue_toml_linked_to_dev_null(self, tmp_path: Path) -> None:
        """Not the package defaults, whose account is ``~/.claude``."""
        queue = tmp_path / "q"
        queue.mkdir()
        (queue / "claude_runner.toml").symlink_to("/dev/null")
        result = _bounded(
            lambda: CliRunner().invoke(app, ["account", "list", "--queue", str(queue)])
        )
        assert result.exit_code == 78
        assert result.stdout == (
            "configuration error: Settings file is a character device, not a regular file: "
            f"{queue / 'claude_runner.toml'}\n"
        )

    def test_a_fifo_at_the_per_queue_name_is_refused(self, tmp_path: Path) -> None:
        queue = tmp_path / "q"
        queue.mkdir()
        os.mkfifo(queue / "claude_runner.toml")
        result = _bounded(
            lambda: CliRunner().invoke(app, ["account", "list", "--queue", str(queue)])
        )
        assert result.exit_code == 78
        assert result.stdout == (
            "configuration error: Settings file is a FIFO, not a regular file: "
            f"{queue / 'claude_runner.toml'}\n"
        )

    def test_a_fifo_as_an_account_file(self, tmp_path: Path) -> None:
        path = tmp_path / PER_ACCOUNT_TOML_NAME
        os.mkfifo(path)
        with pytest.raises(ConfigError) as excinfo:
            _bounded(lambda: load_account_policy(str(tmp_path)))
        assert str(excinfo.value) == f"{path} is a FIFO, not a regular file"
        assert excinfo.value.transient is False


def _fail_open(monkeypatch: pytest.MonkeyPatch, path: Path, code: int) -> None:
    """Make opening ``path`` raise ``OSError(code)``, as a failing mount would."""
    real_open = os.open

    def failing_open(file: object, *args: object, **kwargs: object) -> int:
        if Path(os.fsdecode(file)) == path:  # type: ignore[arg-type]
            raise OSError(code, os.strerror(code))
        return real_open(file, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", failing_open)


DEEP_TOML = "x = " + "[" * 5000 + "]" * 5000 + "\n"
"""Valid TOML that ``tomllib`` cannot parse without exceeding the recursion limit."""


class TestAccountFileErrno:
    def test_a_symlink_loop_on_the_file_is_content(self, tmp_path: Path) -> None:
        path = tmp_path / PER_ACCOUNT_TOML_NAME
        path.symlink_to(path)
        error = _account_error(tmp_path)
        assert str(error) == f"Cannot read {path}: {LOOP}: '{path}'"
        assert error.transient is False

    def test_a_symlink_loop_on_the_config_dir_is_content(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "acct"
        config_dir.symlink_to(config_dir)
        path = config_dir / PER_ACCOUNT_TOML_NAME
        error = _account_error(config_dir)
        assert str(error) == f"Cannot read {path}: {LOOP}: '{path}'"
        assert error.transient is False

    def test_a_name_too_long_is_content(self, tmp_path: Path) -> None:
        config_dir = tmp_path / LONG_NAME
        path = config_dir / PER_ACCOUNT_TOML_NAME
        error = _account_error(config_dir)
        assert str(error) == f"Cannot read {path}: {TOO_LONG}: '{path}'"
        assert error.transient is False

    @pytest.mark.parametrize("code", [errno.EIO, errno.ESTALE, errno.ENOTCONN, errno.EPERM])
    def test_an_injected_read_error_may_come_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
    ) -> None:
        path = tmp_path / PER_ACCOUNT_TOML_NAME
        path.write_text("max_concurrency = 2\n", encoding="utf-8")
        _fail_open(monkeypatch, path, code)
        error = _account_error(tmp_path)
        assert str(error) == f"Cannot read {path}: [Errno {code}] {os.strerror(code)}"
        assert error.transient is True

    def test_nesting_too_deep_is_content(self, tmp_path: Path) -> None:
        path = tmp_path / PER_ACCOUNT_TOML_NAME
        path.write_text(DEEP_TOML, encoding="utf-8")
        error = _account_error(tmp_path)
        assert str(error) == f"{path} is nested too deeply to load"
        assert error.transient is False

    def test_a_nul_in_the_config_dir_is_content_and_does_not_raise(self) -> None:
        """``resolve_accounts`` reports it as a broken account; the validator
        refuses it at load, so only settings built without validation carry it."""
        settings = load_settings(None).model_copy(
            update={
                "accounts": [
                    AccountSettings.model_construct(name="personal", config_dir="/tmp/x\x00y")
                ]
            }
        )
        resolution = resolve_accounts(settings)
        assert resolution.broken == (
            BrokenAccount(
                name="personal",
                path="/tmp/x\x00y/runner-account.toml",
                error="config_dir '/tmp/x\\x00y' contains a NUL byte",
                transient=False,
            ),
        )


def test_start_refuses_a_config_through_a_file_with_78(tmp_path: Path) -> None:
    queue = tmp_path / "q"
    queue.mkdir()
    config = tmp_path / "passwd" / "x.toml"
    config.parent.write_text("", encoding="utf-8")
    result = _start_with(["--queue", str(queue), "--config", str(config)])
    assert result.exit_code == 78
    assert result.stdout == (
        f"supervisor not started: {config.parent} is not a directory, so {config} cannot be read\n"
    )


def test_a_dangling_queue_toml_symlink_is_not_the_defaults(tmp_path: Path) -> None:
    """``<queue>/claude_runner.toml`` symlinked to a mount not up yet: exit 1,
    as for ``--config``, instead of the defaults and their ~/.claude account."""
    queue = tmp_path / "q"
    queue.mkdir()
    target = tmp_path / "unmounted" / "claude_runner.toml"
    (queue / "claude_runner.toml").symlink_to(target)
    result = CliRunner().invoke(app, ["account", "list", "--queue", str(queue)])
    assert result.exit_code == 1
    assert result.stdout == (
        f"configuration error: {queue / 'claude_runner.toml'} is a symlink to {target}, "
        "which does not exist\n"
    )


@pytest.mark.parametrize(
    ("argv", "lead"),
    [
        (["account", "list"], "configuration error"),
        (["supervisor", "start"], "supervisor not started"),
    ],
    ids=["account-list", "supervisor-start"],
)
def test_a_directory_at_the_queue_toml_is_not_the_defaults(
    tmp_path: Path, argv: list[str], lead: str
) -> None:
    """``<queue>/claude_runner.toml`` that is a directory: exit 78, as for
    ``--config``, instead of the defaults and their ~/.claude account."""
    queue = tmp_path / "q"
    queue.mkdir()
    (queue / "claude_runner.toml").mkdir()
    with (
        patch("claude_task_runner.cli.supervisor_cmd.configure_logging"),
        patch(
            "claude_task_runner.cli.supervisor_cmd.start_daemon",
            side_effect=AssertionError("start_daemon reached"),
        ),
    ):
        result = CliRunner().invoke(app, [*argv, "--queue", str(queue)])
    assert result.exit_code == 78
    assert result.stdout == (
        f"{lead}: Settings file is a directory: {queue / 'claude_runner.toml'}\n"
    )
    assert not (queue / ".claude_task_runner").exists()


def test_a_sighup_with_nesting_too_deep_keeps_the_old_config(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    current = load_settings(None)
    config = tmp_path / "claude_runner.toml"
    config.write_text(DEEP_TOML, encoding="utf-8")
    with caplog.at_level(logging.INFO, logger=DAEMON_LOGGER):
        new_settings, pending = _apply_sighup_reload(
            current=current, config_path=config, prior_pending=4, pending_count_fn=lambda: 99
        )
    assert new_settings is current
    assert pending == 4
    assert [(r.levelno, r.getMessage()) for r in caplog.records if r.name == DAEMON_LOGGER] == [
        (
            logging.ERROR,
            f"SIGHUP received: reload failed ({config} is nested too deeply to load); "
            "keeping old config",
        )
    ]


def _start_with(argv: list[str]) -> object:
    with (
        patch("claude_task_runner.cli.supervisor_cmd.configure_logging"),
        patch(
            "claude_task_runner.cli.supervisor_cmd.start_daemon",
            side_effect=AssertionError("start_daemon reached"),
        ),
    ):
        return CliRunner().invoke(app, ["supervisor", "start", *argv])


class TestStartExitStatus:
    def test_start_config_not_utf8_exits_78(self, tmp_path: Path) -> None:
        queue = tmp_path / "q"
        queue.mkdir()
        config = tmp_path / "broken.toml"
        config.write_bytes(NON_UTF8)
        result = _start_with(["--queue", str(queue), "--config", str(config)])
        assert result.exit_code == 78
        assert result.stdout == (
            f"supervisor not started: Invalid TOML in {config}: {UTF8_ERROR}\n"
        )

    def test_start_config_that_is_a_directory_exits_78(self, tmp_path: Path) -> None:
        queue = tmp_path / "q"
        queue.mkdir()
        result = _start_with(["--queue", str(queue), "--config", str(tmp_path)])
        assert result.exit_code == 78
        assert result.stdout == (
            f"supervisor not started: Settings file is a directory: {tmp_path}\n"
        )

    def test_start_config_missing_exits_1(self, tmp_path: Path) -> None:
        queue = tmp_path / "q"
        queue.mkdir()
        missing = tmp_path / "not-mounted-yet.toml"
        result = _start_with(["--queue", str(queue), "--config", str(missing)])
        assert result.exit_code == 1
        assert result.stdout == f"supervisor not started: Settings file not found: {missing}\n"
        assert not (queue / ".claude_task_runner").exists()

    def test_an_account_dir_that_may_come_back_exits_1(self, tmp_path: Path) -> None:
        accounts = _accounts(tmp_path)
        unmounted = tmp_path / "unmounted"
        accounts["work"] = str(unmounted)
        queue = _queue(tmp_path, accounts)
        result = _start(queue)
        assert result.exit_code == 1
        assert result.stdout == (
            "supervisor not started: 1 account cannot take tasks:\n"
            f"  work: config_dir {unmounted} does not exist\n"
            "Each may clear up by itself (a config_dir on a mount that is not up yet, say), "
            "so this start exits 1 and systemd tries again; fix it if it does not.\n"
            f"{REFUSAL_TAIL}\n"
        )
        assert not (queue / ".claude_task_runner").exists()

    def test_any_content_error_among_them_exits_78(self, tmp_path: Path) -> None:
        accounts = _accounts(tmp_path)
        (Path(accounts["personal"]) / PER_ACCOUNT_TOML_NAME).write_text(BAD_TOML, encoding="utf-8")
        accounts["work"] = str(tmp_path / "unmounted")
        result = _start(_queue(tmp_path, accounts))
        assert result.exit_code == 78
        assert "Each may clear up by itself" not in result.stdout

    def test_a_claude_config_dir_with_an_unknown_user_refuses_start(self, tmp_path: Path) -> None:
        """``[claude].config_dir`` is validated even beside explicit
        ``[[accounts]]``: the usage source of a one-account queue reads it."""
        queue = _queue(tmp_path, _accounts(tmp_path))
        toml = queue / "claude_runner.toml"
        toml.write_text(
            f'[claude]\nconfig_dir = "{GONE}"\n\n' + toml.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        result = _start(queue)
        assert result.exit_code == 78
        assert result.stdout.startswith(
            "supervisor not started: Settings validation failed: 1 validation error for "
            "Settings\nclaude.config_dir\n  Value error, [claude].config_dir "
            f"'{GONE}' cannot be expanded: Could not determine home directory. "
        )


@pytest.mark.allow_self_signal
@pytest.mark.usefixtures("private_global_lock")
def test_a_sighup_with_a_non_utf8_file_keeps_the_supervisor_running(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    qd, config = single_account_queue(tmp_path, "")

    def corrupt() -> None:
        config.write_bytes(NON_UTF8)

    with caplog.at_level(logging.INFO, logger=DAEMON_LOGGER):
        # Returns only after all its ticks: the reload did not end the loop.
        run_daemon(
            qd,
            config,
            pending_count_fn=sighup_in_second_tick(corrupt),
            install_signal_handlers=True,
            max_ticks=5,
        )
    errors = [
        r.getMessage()
        for r in caplog.records
        if r.name == DAEMON_LOGGER and r.levelno == logging.ERROR
    ]
    assert errors == [
        f"SIGHUP received: reload failed (Invalid TOML in {config}: {UTF8_ERROR}); "
        "keeping old config"
    ]


# ---------------------------------------------------------------------------
# The validator accepts every config_dir that can be expanded
# ---------------------------------------------------------------------------


def _current_user() -> str:
    return pwd.getpwuid(os.getuid()).pw_name


@pytest.mark.parametrize(
    "config_dir",
    ["", "~", "~/.claude", "~{user}/.claude", "rel/dir", "$HOME/.claude", "/srv/claude"],
)
def test_an_expandable_config_dir_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config_dir: str
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    value = config_dir.format(user=_current_user())
    queue = _queue(tmp_path, {"personal": value})
    settings = load_settings(queue / "claude_runner.toml")
    assert settings.accounts[0].config_dir == value
    if value:
        expected = {
            "~": tmp_path,
            "~/.claude": tmp_path / ".claude",
            f"~{_current_user()}/.claude": Path(pwd.getpwnam(_current_user()).pw_dir) / ".claude",
            "rel/dir": Path("rel/dir"),
            # A variable is not expanded: config_dir is a path, not a shell word.
            "$HOME/.claude": Path("$HOME/.claude"),
            "/srv/claude": Path("/srv/claude"),
        }[value]
        assert expand_config_dir(value) == expected


# ---------------------------------------------------------------------------
# The other commands print a configuration error without a traceback
# ---------------------------------------------------------------------------


DRAIN_LEAD = (
    "cannot load the settings for the default --timeout (pass --timeout <seconds>, "
    "or --no-wait, to drain without them)"
)

# argv after the command words, the lead its error starts with, and where it
# prints: every command that loads the queue TOML, so each one is pinned to
# load_settings_or_exit. test_cli_settings_gate.py keeps a new command from
# calling load_settings itself.
SETTINGS_COMMANDS = [
    (["account", "list", "--queue", "{queue}"], "configuration error", "stdout"),
    (["account", "list", "--queue", "{queue}", "--json"], "configuration error", "json"),
    (["account", "pause", "personal", "--queue", "{queue}"], "configuration error", "stdout"),
    (
        ["account", "pause", "personal", "--queue", "{queue}", "--json"],
        "configuration error",
        "json",
    ),
    (["account", "resume", "personal", "--queue", "{queue}"], "configuration error", "stdout"),
    (
        ["account", "resume", "personal", "--queue", "{queue}", "--json"],
        "configuration error",
        "json",
    ),
    (["supervisor", "start", "--queue", "{queue}"], "supervisor not started", "stdout"),
    (["supervisor", "status", "--queue", "{queue}"], "configuration error", "stdout"),
    (["supervisor", "status", "--queue", "{queue}", "--json"], "configuration error", "json"),
    (["supervisor", "drain", "--queue", "{queue}"], DRAIN_LEAD, "stdout"),
    (["doctor", "--queue", "{queue}"], "configuration error", "stdout"),
    (["doctor", "--queue", "{queue}", "--json"], "configuration error", "json"),
    (["usage"], "configuration error", "stdout"),
    (["install", "--queue", "{queue}", "--yes"], "not installed", "stdout"),
    (["install", "uninstall", "--yes"], "configuration error", "stdout"),
    (
        ["queue", "list", "--queue", "{queue}"],
        "cannot load the queue's settings to check effort levels",
        "stdout",
    ),
    (
        ["queue", "list", "--queue", "{queue}", "--json"],
        "cannot load the queue's settings to check effort levels",
        "json",
    ),
    (
        ["queue", "add", "--queue", "{queue}", "--id", "t1", "--title", "T", "--prompt", "p"],
        "configuration error",
        "stdout",
    ),
    (["queue", "backfill-working-dir", "--queue", "{queue}"], "configuration error", "stdout"),
    (
        ["queue", "backfill-working-dir", "--queue", "{queue}", "--json"],
        "configuration error",
        "json",
    ),
    (["queue", "force-dispatch", "t1", "--queue", "{queue}"], "configuration error", "stdout"),
    (
        ["queue", "force-dispatch", "t1", "--queue", "{queue}", "--json"],
        "configuration error",
        "json",
    ),
    (["worktree", "reclaim", "--queue", "{queue}"], "configuration error", "stderr"),
    (["worktree", "reclaim", "--queue", "{queue}", "--json"], "configuration error", "json"),
    (["watchdog", "register", "--queue", "{queue}"], "register failed", "stderr"),
]


_SETTINGS_PARAMS = [
    pytest.param(argv, lead, stream, id=" ".join(argv).replace(" --queue {queue}", ""))
    for argv, lead, stream in SETTINGS_COMMANDS
]


def _invoke_settings_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, argv: list[str], config: Path, pid: int
) -> object:
    """Run ``argv`` with ``--config config`` on a queue under ``tmp_path``.

    The queue has ``todo/`` and a supervisor PID file naming ``pid``, so
    each command gets as far as loading its settings; nothing may run
    after that."""
    monkeypatch.setenv("HOME", str(tmp_path))
    queue = tmp_path / "q"
    (queue / "todo").mkdir(parents=True, exist_ok=True)
    (queue / ".claude_task_runner").mkdir(exist_ok=True)
    (queue / ".claude_task_runner" / "supervisor.pid").write_text(f"{pid}\n")
    args = [a.format(queue=queue) for a in argv] + ["--config", str(config)]
    with (
        patch("claude_task_runner.cli.supervisor_cmd.configure_logging"),
        patch(
            "claude_task_runner.cli.supervisor_cmd.start_daemon",
            side_effect=AssertionError("start_daemon reached"),
        ),
        patch(
            "claude_task_runner.cli.install_cmd._detect_init_system",
            side_effect=AssertionError("install went on"),
        ),
        patch(
            "claude_task_runner.cli.supervisor_cmd._send_signal",
            side_effect=AssertionError("a process was signalled"),
        ),
    ):
        return CliRunner().invoke(app, args)


def _assert_reported(result: object, lead: str, stream: str, error: str, *, exact: bool) -> None:
    """``lead: error`` on ``stream`` alone, with no traceback."""
    assert isinstance(result.exception, SystemExit), result.exception
    if stream == "json":
        payload = json.loads(result.stdout)
        assert set(payload) == {"ok", "error"}
        assert payload["ok"] is False
        text = payload["error"]
    else:
        text = getattr(result, stream)
        other = result.stderr if stream == "stdout" else result.stdout
        assert other == ""
    message = f"{lead}: {error}"
    if exact:
        assert text.rstrip("\n") == message
    else:
        assert text.startswith(message)
    assert "Traceback" not in result.output


@pytest.mark.parametrize(("argv", "lead", "stream"), _SETTINGS_PARAMS)
def test_every_command_reports_wrong_content_with_78(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    live_worker_pid: int,
    argv: list[str],
    lead: str,
    stream: str,
) -> None:
    config = tmp_path / "claude_runner.toml"
    config.write_text(f'[[accounts]]\nname = "personal"\nconfig_dir = "{GONE}"\n', encoding="utf-8")
    result = _invoke_settings_command(tmp_path, monkeypatch, argv, config, live_worker_pid)
    assert result.exit_code == 78, result.output
    _assert_reported(result, lead, stream, UNKNOWN_USER_ERROR, exact=False)


@pytest.mark.parametrize(("argv", "lead", "stream"), _SETTINGS_PARAMS)
def test_every_command_reports_a_file_that_may_come_back_with_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    live_worker_pid: int,
    argv: list[str],
    lead: str,
    stream: str,
) -> None:
    missing = tmp_path / "not-mounted-yet.toml"
    result = _invoke_settings_command(tmp_path, monkeypatch, argv, missing, live_worker_pid)
    assert result.exit_code == 1, result.output
    _assert_reported(result, lead, stream, f"Settings file not found: {missing}", exact=True)


# ---------------------------------------------------------------------------
# install refuses to install a supervisor that would not start
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("content_too", [False, True], ids=["transient-only", "mixed"])
def test_install_exits_1_only_when_every_problem_may_clear_up(
    tmp_path: Path, content_too: bool
) -> None:
    """As ``supervisor start`` would: a config_dir that is not there yet (a
    mount not up) is exit 1, but one wrong file among them is exit 78."""
    accounts = _accounts(tmp_path)
    personal = Path(accounts["personal"]) / PER_ACCOUNT_TOML_NAME
    if content_too:
        personal.write_text(BAD_TOML, encoding="utf-8")
    unmounted = tmp_path / "unmounted"
    accounts["work"] = str(unmounted)
    queue = _queue(tmp_path, accounts)
    with patch(
        "claude_task_runner.cli.install_cmd._detect_init_system",
        side_effect=AssertionError("install went on"),
    ):
        result = CliRunner().invoke(app, ["install", "--yes", "--queue", str(queue)])
    may_clear_up = (
        "Each may clear up by itself (a config_dir on a mount that is not up yet, say), "
        "but the supervisor would not start until it does.\n"
    )
    tail = (
        "Fix each one (a runner-account.toml can also be deleted for the defaults), then run "
        "install again. Once the supervisor runs, a problem like this only stops new tasks on "
        "its account.\n"
    )
    if content_too:
        assert result.exit_code == 78
        assert result.stdout == (
            "not installed: 2 accounts cannot take tasks:\n"
            f"  personal: Invalid TOML in {personal}: {PARSE_ERROR}\n"
            f"  work: config_dir {unmounted} does not exist\n" + tail
        )
    else:
        assert result.exit_code == 1
        assert result.stdout == (
            "not installed: 1 account cannot take tasks:\n"
            f"  work: config_dir {unmounted} does not exist\n" + may_clear_up + tail
        )


def test_install_refuses_a_broken_account_file(tmp_path: Path) -> None:
    accounts = _accounts(tmp_path)
    path = Path(accounts["personal"]) / PER_ACCOUNT_TOML_NAME
    path.write_text(BAD_TOML, encoding="utf-8")
    queue = _queue(tmp_path, accounts)
    with patch(
        "claude_task_runner.cli.install_cmd._detect_init_system",
        side_effect=AssertionError("install went on"),
    ):
        result = CliRunner().invoke(app, ["install", "--yes", "--queue", str(queue)])
    assert result.exit_code == 78
    assert result.stdout == (
        "not installed: 1 account cannot take tasks:\n"
        f"  personal: Invalid TOML in {path}: {PARSE_ERROR}\n"
        "Fix each one (a runner-account.toml can also be deleted for the defaults), then run "
        "install again. Once the supervisor runs, a problem like this only stops new tasks on "
        "its account.\n"
    )
