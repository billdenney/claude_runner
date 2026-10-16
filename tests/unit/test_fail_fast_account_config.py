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

import logging
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from claude_task_runner.cli import app, watchdog_cmd
from claude_task_runner.config.loader import (
    CONFIG_ERROR_EXIT_STATUS,
    PER_ACCOUNT_TOML_NAME,
    ConfigError,
    load_settings,
)
from claude_task_runner.config.paths import ConfigDirError, expand_config_dir
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

BAD_TOML = "max_concurrency 5\n"
PARSE_ERROR = "Expected '=' after a key in a key/value pair (at line 1, column 17)"
GONE = "~no-such-user-ctr-test/.claude"
"""A ``config_dir`` whose ``~user`` names no user."""

REFUSAL_TAIL = (
    "Fix each file, or delete it for the defaults, then start the supervisor again. "
    "Once the supervisor runs, a file that breaks only stops new tasks on its account."
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
            "supervisor not started: the runner-account.toml of 1 account does not load:\n"
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
            "supervisor not started: the runner-account.toml of 2 accounts does not load:\n"
            f"  personal: Invalid TOML in {personal}: {PARSE_ERROR}\n"
            f"  work: {work} is empty; delete it for the defaults, or write the settings "
            "it should hold\n"
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
            f"SIGHUP received: reload failed ({_unknown_user_error('default')}"
        )
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

    def test_claude_init_skips_it(self, caplog: pytest.LogCaptureFixture) -> None:
        from claude_task_runner.claude_init import ensure_initialized

        with caplog.at_level(logging.WARNING, logger="claude_task_runner.claude_init"):
            assert ensure_initialized(GONE, "/tmp") is False
        assert [r.getMessage() for r in caplog.records] == [
            f"ensure_initialized: config_dir '{GONE}' cannot be expanded: "
            "Could not determine home directory."
        ]

    def test_the_doctor_fails_the_account(self, tmp_path: Path) -> None:
        from claude_task_runner.config.schema import AccountSettings
        from claude_task_runner.doctor.checks import CheckStatus, check_accounts

        gone = AccountSettings.model_construct(name="personal", config_dir=GONE, linux_user=None)
        settings = load_settings(None).model_copy(update={"accounts": [gone]})
        result = check_accounts(settings)
        assert result.status is CheckStatus.FAIL
        assert (
            f"personal: config_dir '{GONE}' cannot be expanded: Could not determine home directory."
        ) in (result.remediation or "")


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
        line = result.stdout.splitlines()[-1]
        assert line.split(" ", 1)[1] == (
            f"watchdog: ERROR queue={queue} the runner-account.toml of 1 account(s) does "
            f"not load, so its supervisor was not restarted: work: Invalid TOML in {path}: "
            f"{PARSE_ERROR}"
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
