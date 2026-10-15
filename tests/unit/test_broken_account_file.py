"""An account whose ``runner-account.toml`` does not load (ADR-0035).

Pins: the account takes no new tasks and counts 0 everywhere, with no
policy standing in for its file; every other account dispatches as usual;
nothing that reads the file crashes. The supervisor logs one ERROR per
error or file change and one INFO when the file loads again, records the
error in ``supervisor.json``, and ``account list``, ``supervisor status``
and the doctor show it.

Most tests use ``personal`` (cap 5) and ``work`` (cap 3) with ``personal``'s
file broken.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_task_runner.clock import FakeClock, RealClock
from claude_task_runner.config.loader import (
    PER_ACCOUNT_TOML_NAME,
    AccountResolution,
    BrokenAccount,
    resolve_accounts,
)
from claude_task_runner.config.schema import Settings
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
from claude_task_runner.runner.in_flight import DispatchSlot
from claude_task_runner.supervisor import persistence as persist_mod
from claude_task_runner.supervisor.daemon import (
    AccountFileWatch,
    TickContext,
    apply_policy_errors,
    run_one_tick,
)
from claude_task_runner.supervisor.persistence import initial_snapshot
from claude_task_runner.supervisor.states import AccountState, SupervisorSnapshot, SupervisorState
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
            BrokenAccount(name="personal", path=str(path), error=_invalid_toml(path)),
        )
        assert resolution.policies == {"work": resolution.accounts[0].policy}
        assert resolution.broken_names == frozenset({"personal"})

    @pytest.mark.parametrize(
        ("content", "error_start"),
        [
            ("[concurrency]\nmax_concurrency = 0\n", "Per-account policy validation failed for "),
            ("[throttle.five_hour]\nx = 1\n", ""),
            (b"\xff\xfe", "Invalid TOML in "),
        ],
        ids=["invalid-value", "retired-throttle", "not-utf8"],
    )
    def test_every_kind_of_failure_is_reported(
        self, tmp_path: Path, two_accounts: dict[str, Path], content: str | bytes, error_start: str
    ) -> None:
        path = two_accounts["personal"] / PER_ACCOUNT_TOML_NAME
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        resolution = resolve_accounts(queue_toml(tmp_path / "q.toml", two_accounts))
        (broken,) = resolution.broken
        assert broken.name == "personal"
        assert str(path) in broken.error
        assert broken.error.startswith(error_start or str(path))

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

    @pytest.mark.parametrize("layout", ["no-file", "no-dir", "dir-is-a-file"])
    def test_a_missing_file_is_not_broken(self, tmp_path: Path, layout: str) -> None:
        config_dir = tmp_path / "acct"
        if layout == "no-file":
            config_dir.mkdir()
        elif layout == "dir-is-a-file":
            config_dir.write_text("", encoding="utf-8")
        resolution = resolve_accounts(queue_toml(tmp_path / "q.toml", {"solo": config_dir}))
        assert resolution.broken == ()
        assert resolution.accounts[0].policy.concurrency.max_concurrency == 1


# ---------------------------------------------------------------------------
# AccountFileWatch: the ERROR and INFO lines
# ---------------------------------------------------------------------------


def _messages(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [
        r.getMessage() for r in caplog.records if r.name == DAEMON_LOGGER and r.levelno == level
    ]


def _error_line(name: str, path: Path, error: str | None = None) -> str:
    return (
        f"account {name!r} takes no new tasks until its runner-account.toml loads: "
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
            "no account's runner-account.toml loads (personal, work): nothing is "
            "dispatched until one is fixed",
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
            ("personal", None, "pinned account 'personal' takes no tasks: "),
            (None, "personal", "session host account 'personal' takes no tasks: "),
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

    def test_with_no_account_loading_the_request_waits(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        _break(two_accounts["personal"])
        _break(two_accounts["work"])
        settings = queue_toml(tmp_path / "q.toml", two_accounts)
        self._request(queue_dir)
        n, _slots = self._consume(queue_dir, settings)
        assert n == 0
        assert fd_mod.request_path(queue_dir, "t00").exists()

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
            f"task t00: pinned account 'personal' takes no tasks: {_invalid_toml(path)}"
        )


# ---------------------------------------------------------------------------
# The supervisor stays up
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("private_global_lock")
class TestSupervisor:
    def test_a_broken_file_at_startup_disables_the_account_without_a_crash(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        qd, config = single_account_queue(tmp_path, "")
        path = _break(tmp_path / "acct")
        with caplog.at_level(logging.INFO, logger=DAEMON_LOGGER):
            run_daemon(qd, config, pending_count_fn=lambda: 0, install_signal_handlers=False)
        assert _messages(caplog, logging.ERROR) == [
            _error_line("default", path),
            "no account's runner-account.toml loads (default): nothing is dispatched "
            "until one is fixed",
        ]
        assert not any("tick_dispatch failed" in r.getMessage() for r in caplog.records)
        assert (
            "concurrency ceiling: 0, the sum of the accounts' max_concurrency (none); "
            "not counted until its runner-account.toml loads: default"
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
            "not counted until its runner-account.toml loads: personal\n"
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
            "not counted until its runner-account.toml loads: personal\n"
        ) in text.stdout
        as_json = invoke_cli(["supervisor", "status", "--queue", str(queue_dir), "--json"])
        assert as_json.exit_code == 0, as_json.output
        assert json.loads(as_json.stdout)["account_errors"] == [
            {"name": "personal", "path": str(path), "error": _invalid_toml(path)}
        ]


class TestDoctor:
    def test_the_account_check_fails_with_the_error(
        self, tmp_path: Path, two_accounts: dict[str, Path]
    ) -> None:
        path = _break(two_accounts["personal"])
        result = check_account_policies(queue_toml(tmp_path / "q.toml", two_accounts))
        assert result.status is CheckStatus.FAIL
        assert result.detail == "1 of 2 per-account policies invalid"
        assert result.remediation == f"personal: {_invalid_toml(path)}"

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 file")
    def test_the_legacy_throttle_check_survives_an_unreadable_file(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        path = two_accounts["personal"] / PER_ACCOUNT_TOML_NAME
        path.chmod(0)
        try:
            settings = queue_toml(tmp_path / "q.toml", two_accounts)
            assert resolve_accounts(settings).broken[0].error == (
                f"Cannot read {path}: [Errno 13] Permission denied: '{path}'"
            )
            result = check_dispatch_pct_legacy(settings, queue_dir)
        finally:
            path.chmod(0o644)
        assert result.status is CheckStatus.PASS
