"""An account whose ``runner-account.toml`` does not load (ADR-0035).

Pins, for a file that breaks while the supervisor runs: the account takes
no new tasks and counts 0 everywhere, with no policy standing in for its
file; every other account dispatches as usual; nothing that reads the file
crashes. The supervisor logs one ERROR per error or file change and one
INFO when the file loads again, records the error in ``supervisor.json``,
and ``account list``, ``supervisor status`` and the doctor show it.
``supervisor start`` refuses to start on one instead
(``test_fail_fast_account_config.py``).

Most tests use ``personal`` (cap 5) and ``work`` (cap 3) with ``personal``'s
file broken.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pydantic
import pytest

from claude_task_runner.clock import FakeClock, RealClock
from claude_task_runner.config.loader import (
    PER_ACCOUNT_TOML_NAME,
    AccountResolution,
    BrokenAccount,
    resolve_accounts,
)
from claude_task_runner.config.schema import AccountSettings, Settings
from claude_task_runner.doctor.checks import (
    CheckStatus,
    check_account_policies,
    check_dispatch_pct_legacy,
)
from claude_task_runner.queue.schema import Task, TaskState
from claude_task_runner.queue.store import (
    queue_runtime_dir,
    state_path_for,
    task_path_for,
    todo_dir,
    write_state_atomic,
    write_task_atomic,
)
from claude_task_runner.runner import force_dispatch as fd_mod
from claude_task_runner.runner.account_dispatch import choose_account
from claude_task_runner.runner.in_flight import DispatchSlot
from claude_task_runner.supervisor import persistence as persist_mod
from claude_task_runner.supervisor.daemon import (
    AccountFileWatch,
    TickContext,
    apply_policy_errors,
    run_one_tick,
)
from claude_task_runner.supervisor.persistence import initial_snapshot
from claude_task_runner.supervisor.states import (
    AccountState,
    InFlightRecord,
    SupervisorSnapshot,
    SupervisorState,
)
from claude_task_runner.usage.models import UsageReading, WindowReading

from ._concurrency_fixtures import (
    DAEMON_LOGGER,
    HeldDispatches,
    dispatch_tick,
    in_flight_by_account,
    invoke_cli,
    make_tasks,
    queue_toml,
    run_daemon,
    single_account_queue,
    write_account_cap,
)

BAD_TOML = "max_concurrency 5\n"
"""Not TOML: a key with no ``=``."""

WAIT = " takes no tasks until this is fixed: "
"""How a decline names an account whose file does not load."""

PARSE_ERROR = "Expected '=' after a key in a key/value pair (at line 1, column 17)"
"""What :mod:`tomllib` says about :data:`BAD_TOML`."""


def _break(config_dir: Path, content: str = BAD_TOML) -> Path:
    path = config_dir / PER_ACCOUNT_TOML_NAME
    path.write_text(content, encoding="utf-8")
    return path


def _invalid_toml(path: Path) -> str:
    return f"Invalid TOML in {path}: {PARSE_ERROR}"


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    return qd


@pytest.fixture
def two_accounts(tmp_path: Path) -> dict[str, Path]:
    """``personal`` capped at 5 and ``work`` at 3, each in its own file."""
    dirs = {"personal": tmp_path / "personal", "work": tmp_path / "work"}
    write_account_cap(dirs["personal"], 5)
    write_account_cap(dirs["work"], 3)
    return dirs


# ---------------------------------------------------------------------------
# The loader: one path that reports the error instead of raising
# ---------------------------------------------------------------------------


class TestResolveAccounts:
    def test_a_broken_file_is_reported_and_the_others_load(
        self, tmp_path: Path, two_accounts: dict[str, Path]
    ) -> None:
        path = _break(two_accounts["personal"])
        resolution = resolve_accounts(queue_toml(tmp_path / "q.toml", two_accounts))
        assert [a.name for a in resolution.accounts] == ["work"]
        assert resolution.accounts[0].policy.concurrency.max_concurrency == 3
        assert resolution.broken == (
            BrokenAccount(name="personal", path=str(path), error=_invalid_toml(path), cause="file"),
        )
        assert resolution.policies == {"work": resolution.accounts[0].policy}
        assert resolution.broken_names == frozenset({"personal"})

    @pytest.mark.parametrize(
        ("content", "error"),
        [
            (
                "[concurrency]\nmax_concurrency = 0\n",
                "Per-account policy validation failed for {path}: 1 validation error for "
                "AccountPolicy\nconcurrency.max_concurrency\n  Input should be greater than "
                "or equal to 1 [type=greater_than_equal, input_value=0, input_type=int]\n"
                "    For further information visit "
                "https://errors.pydantic.dev/{pydantic}/v/greater_than_equal",
            ),
            (
                "[throttle.five_hour]\nx = 1\n",
                "{path}: [throttle.*] is gone in ADR-0022; rename to [dispatch_pct.*]. "
                "Mapping: day = {{fivehr_slowdown_pct, fivehr_stop_pct}}, night = "
                "{{fivehr_slowdown_pct, fivehr_stop_pct, time_start, time_end}}, week = "
                "{{early_pct, eow_pct, eow_time_switch}}. See "
                "docs/cheatsheet.md#migration-from-throttle and "
                "docs/decisions/0022-dispatch-pct-trace-following.md.",
            ),
            (
                b"\xff\xfe",
                "Invalid TOML in {path}: 'utf-8' codec can't decode byte 0xff in position 0: "
                "invalid start byte",
            ),
            (
                b"",
                "{path} is empty; delete it for the defaults, or write the settings it should hold",
            ),
        ],
        ids=["invalid-value", "retired-throttle", "not-utf8", "empty"],
    )
    def test_every_kind_of_failure_is_reported(
        self, tmp_path: Path, two_accounts: dict[str, Path], content: str | bytes, error: str
    ) -> None:
        path = two_accounts["personal"] / PER_ACCOUNT_TOML_NAME
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        resolution = resolve_accounts(queue_toml(tmp_path / "q.toml", two_accounts))
        pydantic_minor = ".".join(pydantic.VERSION.split(".")[:2])
        assert resolution.broken == (
            BrokenAccount(
                name="personal",
                path=str(path),
                error=error.format(path=path, pydantic=pydantic_minor),
                cause="file",
            ),
        )

    def test_a_dangling_symlink_is_broken(
        self, tmp_path: Path, two_accounts: dict[str, Path]
    ) -> None:
        path = two_accounts["personal"] / PER_ACCOUNT_TOML_NAME
        path.unlink()
        target = tmp_path / "saved-elsewhere.toml"
        path.symlink_to(target)
        resolution = resolve_accounts(queue_toml(tmp_path / "q.toml", two_accounts))
        assert resolution.broken[0].error == (
            f"{path} is a symlink to {target}, which does not exist"
        )
        # The target may appear yet (a file saved elsewhere, a mount).
        assert resolution.broken[0].transient is True
        assert resolution.broken[0].cause == "file"

    def test_a_home_removed_while_running_is_broken(
        self, tmp_path: Path, two_accounts: dict[str, Path]
    ) -> None:
        """The queue TOML refuses a ``~user`` that names no user when it
        loads, so this is a user removed after the supervisor loaded it,
        stood in for by settings built without validation."""
        settings = queue_toml(tmp_path / "q.toml", two_accounts)
        gone = AccountSettings.model_construct(
            name="personal", config_dir="~no-such-user-ctr-test/.claude", linux_user=None
        )
        settings = settings.model_copy(update={"accounts": [gone, settings.accounts[1]]})
        resolution = resolve_accounts(settings)
        assert [a.name for a in resolution.accounts] == ["work"]
        assert resolution.broken == (
            BrokenAccount(
                name="personal",
                path="~no-such-user-ctr-test/.claude/runner-account.toml",
                error=(
                    "config_dir '~no-such-user-ctr-test/.claude' cannot be expanded: "
                    "Could not determine home directory."
                ),
                cause="config_dir",
            ),
        )
        assert check_dispatch_pct_legacy(settings, tmp_path).status is CheckStatus.PASS

    def test_a_file_that_cannot_be_read_is_reported(
        self, tmp_path: Path, two_accounts: dict[str, Path]
    ) -> None:
        path = two_accounts["personal"] / PER_ACCOUNT_TOML_NAME
        path.unlink()
        path.mkdir()
        resolution = resolve_accounts(queue_toml(tmp_path / "q.toml", two_accounts))
        assert resolution.broken[0].error == (
            f"Cannot read {path}: [Errno 21] Is a directory: '{path}'"
        )
        # A directory where the file belongs does not go away by itself.
        assert resolution.broken[0].transient is False
        assert resolution.broken[0].cause == "file"

    def test_a_config_dir_without_the_file_is_the_defaults(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "acct"
        config_dir.mkdir()
        resolution = resolve_accounts(queue_toml(tmp_path / "q.toml", {"solo": config_dir}))
        assert resolution.broken == ()
        assert resolution.accounts[0].policy.concurrency.max_concurrency == 1

    @pytest.mark.parametrize("linked", [False, True], ids=["absent", "symlink-to-nothing"])
    def test_a_config_dir_that_does_not_exist_is_broken_but_may_come_back(
        self, tmp_path: Path, linked: bool
    ) -> None:
        """A config_dir on a mount that is not up yet: transient, so a start
        exits 1 and is retried."""
        config_dir = tmp_path / "acct"
        if linked:
            config_dir.symlink_to(tmp_path / "unmounted")
        resolution = resolve_accounts(queue_toml(tmp_path / "q.toml", {"solo": config_dir}))
        assert resolution.broken == (
            BrokenAccount(
                name="solo",
                path=str(config_dir / PER_ACCOUNT_TOML_NAME),
                error=f"config_dir {config_dir} does not exist",
                cause="config_dir",
                transient=True,
            ),
        )
        assert resolution.only_transient is True

    @pytest.mark.parametrize("depth", [0, 2], ids=["config-dir", "ancestor"])
    def test_a_config_dir_through_a_file_is_broken(self, tmp_path: Path, depth: int) -> None:
        """The error names the path that is not a directory, the config_dir
        itself or an ancestor of it."""
        not_a_dir = tmp_path / "acct"
        not_a_dir.write_text("", encoding="utf-8")
        config_dir = not_a_dir.joinpath(*["sub"] * depth)
        path = config_dir / PER_ACCOUNT_TOML_NAME
        resolution = resolve_accounts(queue_toml(tmp_path / "q.toml", {"solo": config_dir}))
        assert resolution.broken == (
            BrokenAccount(
                name="solo",
                path=str(path),
                error=f"{not_a_dir} is not a directory, so {path} cannot be read",
                cause="config_dir",
                transient=False,
            ),
        )
        assert resolution.only_transient is False


class TestLinuxUser:
    """An account whose ``linux_user`` needs ``sudo`` while ``sudo`` is not on
    PATH cannot start a worker, so it is a broken account like one whose file
    does not load: it takes no tasks, and ``supervisor start`` refuses."""

    def _with_linux_user(self, settings: Settings, name: str, linux_user: str) -> Settings:
        return settings.model_copy(
            update={
                "accounts": [
                    a.model_copy(update={"linux_user": linux_user}) if a.name == name else a
                    for a in settings.accounts
                ]
            }
        )

    @pytest.mark.parametrize(
        ("linux_user", "sudo", "broken"),
        [
            ("bill-work", None, True),
            ("bill-work", "/usr/bin/sudo", False),
            ("bill", None, False),
        ],
        ids=["another-user-no-sudo", "another-user-sudo", "own-user-no-sudo"],
    )
    def test_resolve_accounts(
        self,
        tmp_path: Path,
        two_accounts: dict[str, Path],
        linux_user: str,
        sudo: str | None,
        broken: bool,
    ) -> None:
        settings = self._with_linux_user(
            queue_toml(tmp_path / "q.toml", two_accounts), "work", linux_user
        )
        with (
            patch("claude_task_runner.config.linux_user.current_username", return_value="bill"),
            patch("claude_task_runner.config.linux_user.shutil.which", return_value=sudo),
        ):
            resolution = resolve_accounts(settings)
        if broken:
            assert resolution.broken == (
                BrokenAccount(
                    name="work",
                    path=str(two_accounts["work"] / PER_ACCOUNT_TOML_NAME),
                    error="linux_user='bill-work' requested but sudo not on PATH",
                    cause="sudo",
                ),
            )
            assert [a.name for a in resolution.accounts] == ["personal"]
        else:
            assert resolution.broken == ()
            assert [a.name for a in resolution.accounts] == ["personal", "work"]


# ---------------------------------------------------------------------------
# AccountFileWatch: the ERROR and INFO lines
# ---------------------------------------------------------------------------


def _messages(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [
        r.getMessage() for r in caplog.records if r.name == DAEMON_LOGGER and r.levelno == level
    ]


def _error_line(name: str, path: Path, error: str | None = None) -> str:
    return (
        f"account {name!r} takes no new tasks until this is fixed: "
        f"{error if error is not None else _invalid_toml(path)}"
    )


class TestAccountFileWatch:
    def _resolve(self, tmp_path: Path, accounts: dict[str, Path]) -> AccountResolution:
        return resolve_accounts(queue_toml(tmp_path / "q.toml", accounts))

    def test_logs_the_error_exactly_once_across_ticks(
        self, tmp_path: Path, two_accounts: dict[str, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        path = _break(two_accounts["personal"])
        watch = AccountFileWatch()
        with caplog.at_level(logging.INFO, logger=DAEMON_LOGGER):
            for _ in range(5):
                watch.observe(self._resolve(tmp_path, two_accounts))
        assert _messages(caplog, logging.ERROR) == [_error_line("personal", path)]
        assert _messages(caplog, logging.INFO) == []

    def test_a_new_error_is_logged_again(
        self, tmp_path: Path, two_accounts: dict[str, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        path = _break(two_accounts["personal"])
        watch = AccountFileWatch()
        with caplog.at_level(logging.ERROR, logger=DAEMON_LOGGER):
            watch.observe(self._resolve(tmp_path, two_accounts))
            _break(two_accounts["personal"], "[concurrency\n")
            watch.observe(self._resolve(tmp_path, two_accounts))
            watch.observe(self._resolve(tmp_path, two_accounts))
        second = (
            f"Invalid TOML in {path}: "
            "Expected ']' at the end of a table declaration (at line 1, column 13)"
        )
        assert _messages(caplog, logging.ERROR) == [
            _error_line("personal", path),
            _error_line("personal", path, second),
        ]

    def test_an_edit_that_leaves_the_same_error_is_logged_again(
        self, tmp_path: Path, two_accounts: dict[str, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        path = _break(two_accounts["personal"])
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
        watch = AccountFileWatch()
        with caplog.at_level(logging.ERROR, logger=DAEMON_LOGGER):
            watch.observe(self._resolve(tmp_path, two_accounts))
            _break(two_accounts["personal"])
            os.utime(path, ns=(2_000_000_000, 2_000_000_000))
            watch.observe(self._resolve(tmp_path, two_accounts))
        assert _messages(caplog, logging.ERROR) == [_error_line("personal", path)] * 2

    def test_a_fixed_file_logs_recovery_once(
        self, tmp_path: Path, two_accounts: dict[str, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        path = _break(two_accounts["personal"])
        watch = AccountFileWatch()
        with caplog.at_level(logging.INFO, logger=DAEMON_LOGGER):
            watch.observe(self._resolve(tmp_path, two_accounts))
            write_account_cap(two_accounts["personal"], 5)
            for _ in range(3):
                watch.observe(self._resolve(tmp_path, two_accounts))
        assert _messages(caplog, logging.INFO) == [
            f"account 'personal' takes tasks again: {path} loads"
        ]
        assert _messages(caplog, logging.ERROR) == [_error_line("personal", path)]

    def test_no_account_loading_says_nothing_dispatches(
        self, tmp_path: Path, two_accounts: dict[str, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        personal = _break(two_accounts["personal"])
        work = _break(two_accounts["work"])
        watch = AccountFileWatch()
        with caplog.at_level(logging.ERROR, logger=DAEMON_LOGGER):
            for _ in range(3):
                watch.observe(self._resolve(tmp_path, two_accounts))
        assert _messages(caplog, logging.ERROR) == [
            _error_line("personal", personal),
            _error_line("work", work),
            "no account can take tasks (personal, work): nothing is dispatched until one is fixed",
        ]

    def test_no_account_loading_is_said_again_after_one_loaded(
        self, tmp_path: Path, two_accounts: dict[str, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        _break(two_accounts["personal"])
        _break(two_accounts["work"])
        watch = AccountFileWatch()
        with caplog.at_level(logging.ERROR, logger=DAEMON_LOGGER):
            watch.observe(self._resolve(tmp_path, two_accounts))
            write_account_cap(two_accounts["work"], 3)
            watch.observe(self._resolve(tmp_path, two_accounts))
            _break(two_accounts["work"])
            watch.observe(self._resolve(tmp_path, two_accounts))
            watch.observe(self._resolve(tmp_path, two_accounts))
        assert [m for m in _messages(caplog, logging.ERROR) if m.startswith("no account")] == [
            "no account can take tasks (personal, work): nothing is dispatched until one is fixed"
        ] * 2

    def test_a_deleted_file_says_the_defaults_apply(
        self, tmp_path: Path, two_accounts: dict[str, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        path = _break(two_accounts["personal"])
        watch = AccountFileWatch()
        with caplog.at_level(logging.INFO, logger=DAEMON_LOGGER):
            watch.observe(self._resolve(tmp_path, two_accounts))
            path.unlink()
            watch.observe(self._resolve(tmp_path, two_accounts))
        assert _messages(caplog, logging.INFO) == [
            f"account 'personal' takes tasks again: {path} does not exist, so the defaults "
            "apply (max_concurrency = 1 and the queue's [dispatch_pct.*])"
        ]

    def test_a_config_dir_that_appears_without_the_file_says_the_defaults_apply(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A mount that comes up late: the account was broken because its
        config_dir did not exist, and the file was never there."""
        config_dir = tmp_path / "acct"
        accounts = {"solo": config_dir}
        watch = AccountFileWatch()
        with caplog.at_level(logging.INFO, logger=DAEMON_LOGGER):
            watch.observe(self._resolve(tmp_path, accounts))
            config_dir.mkdir()
            watch.observe(self._resolve(tmp_path, accounts))
        assert _messages(caplog, logging.ERROR)[0] == _error_line(
            "solo",
            config_dir / PER_ACCOUNT_TOML_NAME,
            f"config_dir {config_dir} does not exist",
        )
        assert _messages(caplog, logging.INFO) == [
            f"account 'solo' takes tasks again: its config_dir can be read, and "
            f"{config_dir / PER_ACCOUNT_TOML_NAME} does not exist, so the defaults apply "
            "(max_concurrency = 1 and the queue's [dispatch_pct.*])"
        ]

    def test_a_config_dir_that_appears_with_the_file_says_it_loads(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        config_dir = tmp_path / "acct"
        accounts = {"solo": config_dir}
        watch = AccountFileWatch()
        with caplog.at_level(logging.INFO, logger=DAEMON_LOGGER):
            watch.observe(self._resolve(tmp_path, accounts))
            write_account_cap(config_dir, 2)
            watch.observe(self._resolve(tmp_path, accounts))
        assert _messages(caplog, logging.INFO) == [
            f"account 'solo' takes tasks again: its config_dir can be read, and "
            f"{config_dir / PER_ACCOUNT_TOML_NAME} loads"
        ]

    @pytest.mark.parametrize(
        ("linux_user_after", "recovery"),
        [
            ("bill-work", "sudo is on PATH"),
            (None, "its workers no longer run as another Linux user, so it needs no sudo"),
        ],
        ids=["sudo-installed", "linux-user-dropped"],
    )
    def test_sudo_coming_back_is_not_called_a_file_that_loads(
        self,
        tmp_path: Path,
        two_accounts: dict[str, Path],
        caplog: pytest.LogCaptureFixture,
        linux_user_after: str | None,
        recovery: str,
    ) -> None:
        """``work`` runs as another Linux user with no sudo on PATH; its file
        loads all along. It recovers when sudo appears, or when the queue
        TOML stops running it as another user."""
        settings = queue_toml(tmp_path / "q.toml", two_accounts)

        def resolve(linux_user: str | None, sudo: str | None) -> AccountResolution:
            accounts = [
                a.model_copy(update={"linux_user": linux_user}) if a.name == "work" else a
                for a in settings.accounts
            ]
            with (
                patch("claude_task_runner.config.linux_user.current_username", return_value="bill"),
                patch("claude_task_runner.config.linux_user.shutil.which", return_value=sudo),
            ):
                resolution = resolve_accounts(settings.model_copy(update={"accounts": accounts}))
                watch.observe(resolution)
            return resolution

        watch = AccountFileWatch()
        with caplog.at_level(logging.INFO, logger=DAEMON_LOGGER):
            resolve("bill-work", None)
            sudo_after = "/usr/bin/sudo" if linux_user_after else None
            resolve(linux_user_after, sudo_after)
        assert _messages(caplog, logging.ERROR) == [
            "account 'work' takes no new tasks until this is fixed: "
            "linux_user='bill-work' requested but sudo not on PATH"
        ]
        assert _messages(caplog, logging.INFO) == [f"account 'work' takes tasks again: {recovery}"]

    def test_a_sudo_problem_is_not_logged_again_when_the_working_directory_changes(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An account with an empty config_dir has no file, so its path is
        ``""``. Only the error identifies its sudo problem: a change in the
        supervisor's working directory, which ``""`` would stat, is not a
        new one."""
        broken = BrokenAccount(
            name="default",
            path="",
            error="linux_user='bill-work' requested but sudo not on PATH",
            cause="sudo",
        )
        resolution = AccountResolution(accounts=(), broken=(broken,))
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        watch = AccountFileWatch()
        with caplog.at_level(logging.ERROR, logger=DAEMON_LOGGER):
            watch.observe(resolution)
            (cwd / "new-file").write_text("x" * 100, encoding="utf-8")
            os.utime(cwd, ns=(5_000_000_000, 5_000_000_000))
            watch.observe(resolution)
        assert _messages(caplog, logging.ERROR) == [
            "account 'default' takes no new tasks until this is fixed: "
            "linux_user='bill-work' requested but sudo not on PATH",
            "no account can take tasks (default): nothing is dispatched until one is fixed",
        ]


# ---------------------------------------------------------------------------
# The snapshot and the throttle decision
# ---------------------------------------------------------------------------

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def _two_account_snapshot() -> SupervisorSnapshot:
    snap = initial_snapshot(since=NOW, account_names=["personal", "work"])
    dispatching = AccountState(
        state=SupervisorState.DISPATCHING, since=NOW, target_concurrency=5, last_reading_at=NOW
    )
    return snap.model_copy(update={"accounts": {"personal": dispatching, "work": dispatching}})


class TestSnapshot:
    def test_policy_error_is_set_and_cleared(
        self, tmp_path: Path, two_accounts: dict[str, Path]
    ) -> None:
        path = _break(two_accounts["personal"])
        settings = queue_toml(tmp_path / "q.toml", two_accounts)
        snap = apply_policy_errors(_two_account_snapshot(), resolve_accounts(settings))
        assert snap.accounts["personal"].policy_error == _invalid_toml(path)
        assert snap.accounts["work"].policy_error is None
        write_account_cap(two_accounts["personal"], 5)
        snap = apply_policy_errors(snap, resolve_accounts(settings))
        assert snap.accounts["personal"].policy_error is None

    def test_a_reading_for_a_broken_account_makes_no_decision(
        self, tmp_path: Path, two_accounts: dict[str, Path]
    ) -> None:
        """At 70% the queue's day band would throttle the account. With its
        policy unknown, nothing is decided: only the capture is recorded."""
        _break(two_accounts["personal"])
        settings = queue_toml(tmp_path / "q.toml", two_accounts)
        settings = settings.model_copy(
            update={"dispatch_pct": settings.dispatch_pct.model_copy(update={"timezone": "UTC"})}
        )
        resolution = resolve_accounts(settings)
        clock = FakeClock(NOW)
        reading = UsageReading(
            captured_at=NOW,
            five_hour=WindowReading(
                utilization_pct=70,
                resets_at_raw="x",
                resets_at=datetime(2026, 10, 15, 15, tzinfo=UTC),
            ),
            seven_day=WindowReading(utilization_pct=10, resets_at_raw="x", resets_at=None),
            account="personal",
        )
        before = _two_account_snapshot()
        after, actions = run_one_tick(
            before,
            TickContext(
                settings=settings,
                poll_result=reading,
                pending_count=3,
                in_flight_count=0,
                account_policies=resolution.policies,
                broken_accounts=resolution.broken_names,
            ),
            clock,
        )
        assert actions == []
        assert after.accounts["personal"] == before.accounts["personal"].model_copy(
            update={"last_capture_at": NOW}
        )
        assert after.accounts["work"] == before.accounts["work"]


# ---------------------------------------------------------------------------
# Dispatch: the broken account gets 0, the others their caps
# ---------------------------------------------------------------------------


class TestUnattributedReading:
    def test_a_broken_account_counts_0_in_the_fallback_cap(
        self, tmp_path: Path, two_accounts: dict[str, Path]
    ) -> None:
        """A reading no account can be named for is decided against the
        queue's ceiling: personal's 5, with work, whose file is broken,
        counting 0 rather than its default of 1."""
        _break(two_accounts["work"])
        settings = queue_toml(tmp_path / "q.toml", two_accounts)
        settings = settings.model_copy(
            update={"dispatch_pct": settings.dispatch_pct.model_copy(update={"timezone": "UTC"})}
        )
        resolution = resolve_accounts(settings)
        reading = UsageReading(
            captured_at=NOW,
            five_hour=WindowReading(
                utilization_pct=10,
                resets_at_raw="x",
                resets_at=datetime(2026, 10, 15, 15, tzinfo=UTC),
            ),
            seven_day=WindowReading(utilization_pct=10, resets_at_raw="x", resets_at=None),
        )
        after, _actions = run_one_tick(
            _two_account_snapshot(),
            TickContext(
                settings=settings,
                poll_result=reading,
                pending_count=3,
                in_flight_count=0,
                account_policies=resolution.policies,
                broken_accounts=resolution.broken_names,
            ),
            FakeClock(NOW),
        )
        assert after.state is SupervisorState.DISPATCHING
        assert after.target_concurrency == 5


class TestDispatch:
    def test_the_other_account_fills_its_cap_and_the_broken_one_gets_nothing(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        _break(two_accounts["personal"])
        settings = queue_toml(tmp_path / "q.toml", two_accounts)
        make_tasks(queue_dir, 10)
        slots: dict[str, DispatchSlot] = {}
        with HeldDispatches() as held:
            dispatch_tick(queue_dir, settings, slots)
            assert in_flight_by_account(slots) == {"work": 3}
            # The next tick after the file is fixed restores it.
            write_account_cap(two_accounts["personal"], 5)
            dispatch_tick(queue_dir, settings, slots)
            assert in_flight_by_account(slots) == {"personal": 5, "work": 3}
            held.release(slots)

    @pytest.mark.parametrize("problem", ["config-dir-removed", "no-sudo-for-linux-user"])
    def test_an_account_that_cannot_start_a_worker_gets_no_tasks(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path], problem: str
    ) -> None:
        """The account is left out when tasks are routed, so tasks go to the
        other account instead of each being parked on this one."""
        settings = queue_toml(tmp_path / "q.toml", two_accounts)
        if problem == "config-dir-removed":
            shutil.rmtree(two_accounts["personal"])
        else:
            settings = settings.model_copy(
                update={
                    "accounts": [
                        a.model_copy(update={"linux_user": "bill-personal"})
                        if a.name == "personal"
                        else a
                        for a in settings.accounts
                    ]
                }
            )
        make_tasks(queue_dir, 10)
        slots: dict[str, DispatchSlot] = {}
        with (
            patch("claude_task_runner.config.linux_user.current_username", return_value="bill"),
            patch("claude_task_runner.config.linux_user.shutil.which", return_value=None),
            HeldDispatches() as held,
        ):
            dispatch_tick(queue_dir, settings, slots)
            assert in_flight_by_account(slots) == {"work": 3}
            held.release(slots)

    def test_a_task_whose_session_is_on_a_broken_account_waits_and_says_why(
        self,
        tmp_path: Path,
        queue_dir: Path,
        two_accounts: dict[str, Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        path = _break(two_accounts["personal"])
        settings = queue_toml(tmp_path / "q.toml", two_accounts)
        make_tasks(queue_dir, 1)
        write_state_atomic(
            TaskState(
                task_id="t00",
                status="failed",
                session_id="s-1",
                session_account="personal",
                last_finished_at=datetime.now(UTC),
            ),
            state_path_for(queue_dir, "t00"),
        )
        slots: dict[str, DispatchSlot] = {}
        with caplog.at_level(logging.INFO), HeldDispatches() as held:
            dispatch_tick(queue_dir, settings, slots)
            held.release(slots)
        assert slots == {}
        assert (
            "dispatch skipped task t00: session affinity blocks dispatch: host account "
            f"'personal'{WAIT}{_invalid_toml(path)}"
        ) in [r.getMessage() for r in caplog.records]

    def test_a_task_pinned_to_a_broken_account_waits_and_says_why(
        self,
        tmp_path: Path,
        queue_dir: Path,
        two_accounts: dict[str, Path],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        path = _break(two_accounts["personal"])
        settings = queue_toml(tmp_path / "q.toml", two_accounts)
        task = Task.model_validate(
            {"id": "t00", "title": "T", "prompt": "do it", "account": "personal"}
        )
        write_task_atomic(task, task_path_for(queue_dir, "t00"))
        slots: dict[str, DispatchSlot] = {}
        with caplog.at_level(logging.INFO), HeldDispatches() as held:
            dispatch_tick(queue_dir, settings, slots)
            held.release(slots)
        assert slots == {}
        assert (
            f"dispatch skipped task t00: pinned account 'personal'{WAIT}{_invalid_toml(path)}"
        ) in [r.getMessage() for r in caplog.records]

    def test_an_unbound_decline_names_the_broken_account(
        self, tmp_path: Path, two_accounts: dict[str, Path]
    ) -> None:
        path = _break(two_accounts["personal"])
        resolution = resolve_accounts(queue_toml(tmp_path / "q.toml", two_accounts))
        work = AccountState(state=SupervisorState.DISPATCHING, since=NOW)
        choice = choose_account(
            task=Task.model_validate({"id": "t00", "title": "T", "prompt": "do it"}),
            accounts={a.name: a for a in resolution.accounts},
            account_states={"work": work},
            in_flight=[
                InFlightRecord(task_id=f"w{i}", account="work", started_at=NOW) for i in range(3)
            ],
            broken={b.name: b.error for b in resolution.broken},
        )
        assert choice.account is None
        assert choice.reason == (
            "no eligible account: personal: takes no tasks until its error is fixed; "
            "work: at capacity (3/3)"
        )
        assert str(path) not in choice.reason

    def test_nothing_dispatches_when_no_account_loads(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        _break(two_accounts["personal"])
        _break(two_accounts["work"])
        settings = queue_toml(tmp_path / "q.toml", two_accounts)
        make_tasks(queue_dir, 3)
        slots: dict[str, DispatchSlot] = {}
        with HeldDispatches() as held:
            dispatch_tick(queue_dir, settings, slots)
            assert slots == {}
            held.release(slots)


class TestForceDispatch:
    def _request(
        self, queue_dir: Path, *, account: str | None = None, session_on: str | None = None
    ) -> None:
        task = Task.model_validate(
            {"id": "t00", "title": "T", "prompt": "do it", "account": account}
        )
        write_task_atomic(task, task_path_for(queue_dir, "t00"))
        if session_on is not None:
            write_state_atomic(
                TaskState(
                    task_id="t00", status="failed", session_id="s-1", session_account=session_on
                ),
                state_path_for(queue_dir, "t00"),
            )
        fd_mod.write_request(queue_dir, "t00")

    def _consume(self, queue_dir: Path, settings: Settings) -> tuple[int, dict[str, DispatchSlot]]:
        slots: dict[str, DispatchSlot] = {}
        with HeldDispatches() as held:
            n = fd_mod.tick_consume(
                queue_dir=queue_dir, settings=settings, clock=RealClock(), in_flight_slots=slots
            )
            held.release(slots)
        return n, slots

    @pytest.mark.parametrize(
        ("pin", "session_on", "reason"),
        [
            ("personal", None, f"pinned account 'personal'{WAIT}"),
            (None, "personal", f"session host account 'personal'{WAIT}"),
        ],
        ids=["pinned", "session"],
    )
    def test_a_request_bound_to_a_broken_account_waits(
        self,
        tmp_path: Path,
        queue_dir: Path,
        two_accounts: dict[str, Path],
        caplog: pytest.LogCaptureFixture,
        pin: str | None,
        session_on: str | None,
        reason: str,
    ) -> None:
        path = _break(two_accounts["personal"])
        settings = queue_toml(tmp_path / "q.toml", two_accounts)
        self._request(queue_dir, account=pin, session_on=session_on)
        with caplog.at_level(logging.WARNING):
            n, _slots = self._consume(queue_dir, settings)
        assert n == 0
        assert fd_mod.request_path(queue_dir, "t00").exists()
        assert f"force-dispatch t00: {reason}{_invalid_toml(path)}; leaving request" in [
            r.getMessage() for r in caplog.records
        ]

    def test_an_unbound_request_goes_to_an_account_that_loads(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        _break(two_accounts["personal"])
        settings = queue_toml(tmp_path / "q.toml", two_accounts)
        self._request(queue_dir)
        n, slots = self._consume(queue_dir, settings)
        assert n == 1
        assert in_flight_by_account(slots) == {"work": 1}

    @pytest.mark.parametrize("concurrency", ["", "max_concurrency = 5"])
    def test_with_no_account_loading_the_request_waits(
        self,
        tmp_path: Path,
        queue_dir: Path,
        two_accounts: dict[str, Path],
        caplog: pytest.LogCaptureFixture,
        concurrency: str,
    ) -> None:
        _break(two_accounts["personal"])
        _break(two_accounts["work"])
        settings = queue_toml(tmp_path / "q.toml", two_accounts, concurrency)
        self._request(queue_dir)
        with caplog.at_level(logging.WARNING):
            n, _slots = self._consume(queue_dir, settings)
        assert n == 0
        assert fd_mod.request_path(queue_dir, "t00").exists()
        assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == [
            "force-dispatch t00: no account can take tasks; leaving request"
        ]

    def test_the_synchronous_path_refuses_with_the_error(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        path = _break(two_accounts["personal"])
        settings = queue_toml(tmp_path / "q.toml", two_accounts)
        self._request(queue_dir, account="personal")
        with pytest.raises(fd_mod.ForceDispatchError) as excinfo:
            fd_mod.dispatch_synchronously(
                task_id="t00", queue_dir=queue_dir, settings=settings, clock=RealClock()
            )
        assert str(excinfo.value) == (
            f"task t00: pinned account 'personal'{WAIT}{_invalid_toml(path)}"
        )


# ---------------------------------------------------------------------------
# The supervisor stays up
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("private_global_lock")
class TestSupervisor:
    def test_the_daemon_itself_survives_a_broken_file_from_its_first_tick(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``supervisor start`` checks the files first, so this is a file that
        breaks between that check and the first tick: the loop still runs."""
        qd, config = single_account_queue(tmp_path, "")
        path = _break(tmp_path / "acct")
        with caplog.at_level(logging.INFO, logger=DAEMON_LOGGER):
            run_daemon(qd, config, pending_count_fn=lambda: 0, install_signal_handlers=False)
        assert _messages(caplog, logging.ERROR) == [
            _error_line("default", path),
            "no account can take tasks (default): nothing is dispatched until one is fixed",
        ]
        assert not any("tick_dispatch failed" in r.getMessage() for r in caplog.records)
        assert (
            "concurrency ceiling: 0, the sum of the accounts' max_concurrency (none); "
            "not counted until fixed: default"
        ) in _messages(caplog, logging.INFO)
        snap = persist_mod.load(persist_mod.supervisor_state_path(qd))
        assert snap is not None
        assert snap.accounts["default"].policy_error == _invalid_toml(path)

    def test_a_file_broken_and_fixed_while_running(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        qd, config = single_account_queue(tmp_path, "")
        path = tmp_path / "acct" / PER_ACCOUNT_TOML_NAME
        calls = {"n": 0}

        def pending_count() -> int:
            # Called once at startup and once per tick: breaks the file in
            # the second tick and fixes it in the fourth.
            calls["n"] += 1
            if calls["n"] == 3:
                _break(tmp_path / "acct")
            if calls["n"] == 5:
                write_account_cap(tmp_path / "acct", 3)
            return 0

        with caplog.at_level(logging.INFO, logger=DAEMON_LOGGER):
            run_daemon(
                qd,
                config,
                pending_count_fn=pending_count,
                install_signal_handlers=False,
                max_ticks=6,
            )
        errors = _messages(caplog, logging.ERROR)
        assert errors[0] == _error_line("default", path)
        assert errors.count(_error_line("default", path)) == 1
        assert (
            _messages(caplog, logging.INFO).count(
                f"account 'default' takes tasks again: {path} loads"
            )
            == 1
        )
        snap = persist_mod.load(persist_mod.supervisor_state_path(qd))
        assert snap is not None
        assert snap.accounts["default"].policy_error is None


# ---------------------------------------------------------------------------
# What the operator sees
# ---------------------------------------------------------------------------


class TestCli:
    def test_account_list_prints_the_error_and_exits_1(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        path = _break(two_accounts["personal"])
        config = tmp_path / "claude_runner.toml"
        queue_toml(config, two_accounts)
        result = invoke_cli(["account", "list", "--config", str(config), "--queue", str(queue_dir)])
        assert result.exit_code == 1, result.output
        assert f"  policy error:    {_invalid_toml(path)}\n" in result.stdout
        assert "  max_concurrency: 3\n" in result.stdout
        assert result.stdout.count("max_concurrency: ") == 1
        assert (
            "queue ceiling: 3, the sum of the accounts' max_concurrency (work 3); "
            "not counted until fixed: personal\n"
        ) in result.stdout

    def test_account_list_json_marks_the_broken_account_and_exits_1(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        path = _break(two_accounts["personal"])
        config = tmp_path / "claude_runner.toml"
        queue_toml(config, two_accounts)
        result = invoke_cli(
            ["account", "list", "--config", str(config), "--queue", str(queue_dir), "--json"]
        )
        assert result.exit_code == 1, result.output
        payload = json.loads(result.stdout)
        rows = {row["name"]: row for row in payload["accounts"]}
        assert list(rows) == ["personal", "work"]
        assert rows["personal"]["policy_error"] == _invalid_toml(path)
        assert rows["personal"]["policy_file"] == str(path)
        assert rows["personal"]["max_concurrency"] is None
        assert rows["personal"]["dispatch_pct"] is None
        assert rows["personal"]["dispatch_cap"] == 0
        assert rows["work"]["policy_error"] is None
        assert rows["work"]["max_concurrency"] == 3
        assert payload["queue_ceiling"]["ceiling"] == 3
        assert payload["queue_ceiling"]["broken_accounts"] == ["personal"]
        assert set(rows["personal"]) == set(rows["work"])
        assert rows["work"]["policy_file"] == str(two_accounts["work"] / PER_ACCOUNT_TOML_NAME)

    def test_account_list_exits_0_when_every_file_loads(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        config = tmp_path / "claude_runner.toml"
        queue_toml(config, two_accounts)
        result = invoke_cli(["account", "list", "--config", str(config), "--queue", str(queue_dir)])
        assert result.exit_code == 0, result.output

    def test_supervisor_status_shows_the_error(
        self, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        path = _break(two_accounts["personal"])
        queue_toml(queue_dir / "claude_runner.toml", two_accounts)
        text = invoke_cli(["supervisor", "status", "--queue", str(queue_dir)], 80)
        assert text.exit_code == 0, text.output
        assert f"Account error:    personal: {_invalid_toml(path)}\n" in text.stdout
        assert (
            "Queue ceiling:    3, the sum of the accounts' max_concurrency (work 3); "
            "not counted until fixed: personal\n"
        ) in text.stdout
        as_json = invoke_cli(["supervisor", "status", "--queue", str(queue_dir), "--json"])
        assert as_json.exit_code == 0, as_json.output
        assert json.loads(as_json.stdout)["account_errors"] == [
            {"name": "personal", "cause": "file", "path": str(path), "error": _invalid_toml(path)}
        ]


class TestDoctor:
    def test_the_account_check_fails_with_the_error(
        self, tmp_path: Path, two_accounts: dict[str, Path]
    ) -> None:
        path = _break(two_accounts["personal"])
        result = check_account_policies(queue_toml(tmp_path / "q.toml", two_accounts))
        assert result.status is CheckStatus.FAIL
        assert result.detail == "1 of 2 accounts cannot take tasks"
        assert result.remediation == f"personal: {_invalid_toml(path)}"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
    def test_the_legacy_throttle_check_survives_an_unreadable_file(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        path = two_accounts["personal"] / PER_ACCOUNT_TOML_NAME
        path.chmod(0)
        try:
            settings = queue_toml(tmp_path / "q.toml", two_accounts)
            broken = resolve_accounts(settings).broken[0]
            assert broken.error == f"Cannot read {path}: [Errno 13] Permission denied: '{path}'"
            assert broken.transient is True
            result = check_dispatch_pct_legacy(settings, queue_dir)
        finally:
            path.chmod(0o644)
        assert result.status is CheckStatus.PASS
