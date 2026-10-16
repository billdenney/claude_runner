"""Per-account concurrency caps and the optional queue-wide cap (ADR-0035).

Pins: each account's free slots under its own ``max_concurrency``, re-read
every tick, are what a tick may dispatch; the queue TOML's
``[concurrency]`` keys are optional caps on top. An explicit
``max_concurrency`` below the accounts' sum binds and is warned about once
per config load and per change of either number; the ceiling is logged at
INFO once per load.

Most tests use two accounts, ``personal`` capped at 5 and ``work`` at 3, so
the omitted-key ceiling is 8 and an explicit 5 binds.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_task_runner.clock import RealClock
from claude_task_runner.config.loader import ConfigError, load_settings, resolve_accounts
from claude_task_runner.config.schema import ConcurrencySettings, Settings
from claude_task_runner.queue.schema import TaskState
from claude_task_runner.queue.store import (
    queue_runtime_dir,
    state_path_for,
    todo_dir,
    write_state_atomic,
)
from claude_task_runner.runner import force_dispatch as fd_mod
from claude_task_runner.runner import orchestrator as orch_mod
from claude_task_runner.runner.account_dispatch import QueueCeiling, dispatch_headroom
from claude_task_runner.runner.in_flight import DispatchSlot, to_in_flight_records
from claude_task_runner.supervisor.daemon import QueueCeilingWatch
from claude_task_runner.supervisor.states import (
    AccountState,
    InFlightRecord,
    SupervisorSnapshot,
    SupervisorState,
)

from ._concurrency_fixtures import (
    DAEMON_LOGGER,
    HeldDispatches,
    busy_slots,
    dispatch_tick,
    dispatched_by_account,
    in_flight_by_account,
    invoke_cli,
    make_tasks,
    queue_toml,
    resolved_account,
    run_daemon,
    sighup_in_second_tick,
    single_account_body,
    single_account_queue,
    write_account_cap,
)

# ---------------------------------------------------------------------------
# Fixtures: a queue whose accounts' caps come from real runner-account.toml
# files, loaded the way the supervisor loads them.
# ---------------------------------------------------------------------------


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
# QueueCeiling: the summary the log and the CLI show
# ---------------------------------------------------------------------------

CAPS = {"personal": 5, "work": 3}
"""The two accounts' own ``max_concurrency``."""


class TestQueueCeiling:
    def test_omitted_is_the_exact_sum_and_never_binds(self) -> None:
        ceiling = QueueCeiling(configured=None, account_caps=CAPS)
        assert ceiling.accounts_total == 8
        assert ceiling.value == 8
        assert ceiling.binds is False

    def test_explicit_lower_value_is_the_ceiling_and_binds(self) -> None:
        ceiling = QueueCeiling(configured=5, account_caps=CAPS)
        assert ceiling.value == 5
        assert ceiling.binds is True

    @pytest.mark.parametrize("configured", [8, 10])
    def test_explicit_value_at_or_above_the_sum_does_not_bind(self, configured: int) -> None:
        ceiling = QueueCeiling(configured=configured, account_caps=CAPS)
        # The effective number: no account runs more than its own cap.
        assert ceiling.value == 8
        assert ceiling.binds is False

    def test_of_reads_each_accounts_own_cap(self) -> None:
        policies = {
            a.name: a.policy for a in (resolved_account("personal", 5), resolved_account("work", 3))
        }
        assert QueueCeiling.of(None, policies) == QueueCeiling(configured=None, account_caps=CAPS)

    def test_one_account_is_that_accounts_cap(self) -> None:
        assert QueueCeiling.of(None, {"default": resolved_account("default", 1).policy}).value == 1
        assert QueueCeiling.of(None, {"default": resolved_account("default", 4).policy}).value == 4

    @pytest.mark.parametrize(
        ("configured", "text"),
        [
            (None, "8, the sum of the accounts' max_concurrency (personal 5 + work 3)"),
            (
                5,
                "5 from [concurrency].max_concurrency, which binds: the accounts' "
                "max_concurrency sum to 8 (personal 5 + work 3)",
            ),
            (
                10,
                "8, the sum of the accounts' max_concurrency (personal 5 + work 3); "
                "[concurrency].max_concurrency = 10 does not bind",
            ),
            (
                8,
                "8, the sum of the accounts' max_concurrency (personal 5 + work 3); "
                "[concurrency].max_concurrency = 8 does not bind",
            ),
        ],
    )
    def test_describe(self, configured: int | None, text: str) -> None:
        assert QueueCeiling(configured=configured, account_caps=CAPS).describe() == text

    def test_as_json(self) -> None:
        assert QueueCeiling(configured=5, account_caps=CAPS).as_json() == {
            "ceiling": 5,
            "max_concurrency": 5,
            "accounts_total": 8,
            "account_caps": {"personal": 5, "work": 3},
            "binds": True,
            "broken_accounts": [],
        }

    def test_as_json_reports_the_effective_ceiling_above_the_sum(self) -> None:
        assert QueueCeiling(configured=10, account_caps=CAPS).as_json() == {
            "ceiling": 8,
            "max_concurrency": 10,
            "accounts_total": 8,
            "account_caps": {"personal": 5, "work": 3},
            "binds": False,
            "broken_accounts": [],
        }


# ---------------------------------------------------------------------------
# dispatch_headroom: free slots per account, within an explicit cap
# ---------------------------------------------------------------------------

ACCOUNTS = [resolved_account("personal", 5), resolved_account("work", 3)]


def _records(**by_account: int) -> list[InFlightRecord]:
    return [
        InFlightRecord(
            task_id=f"{account}-{i}",
            account=account,
            started_at=datetime(2026, 10, 15, tzinfo=UTC),
        )
        for account, n in by_account.items()
        for i in range(n)
    ]


class TestDispatchHeadroom:
    def test_nothing_in_flight_is_the_sum(self) -> None:
        assert dispatch_headroom(ACCOUNTS, [], None) == 8

    def test_each_account_adds_only_its_own_free_slots(self) -> None:
        assert dispatch_headroom(ACCOUNTS, _records(personal=5, work=1), None) == 2

    def test_an_account_over_its_cap_does_not_take_the_others_slots(self) -> None:
        # personal's cap lowered to 2 while 5 of its tasks run.
        lowered = [resolved_account("personal", 2), resolved_account("work", 3)]
        assert dispatch_headroom(lowered, _records(personal=5), None) == 3

    def test_an_undeclared_accounts_tasks_count_only_against_an_explicit_cap(self) -> None:
        in_flight = _records(retired=4)
        assert dispatch_headroom(ACCOUNTS, in_flight, None) == 8
        assert dispatch_headroom(ACCOUNTS, in_flight, 10) == 6

    def test_an_explicit_cap_counts_every_in_flight_task(self) -> None:
        assert dispatch_headroom(ACCOUNTS, [], 5) == 5
        assert dispatch_headroom(ACCOUNTS, _records(personal=3), 5) == 2
        assert dispatch_headroom(ACCOUNTS, _records(personal=3, work=2), 5) == 0
        # Never negative, even past the cap.
        assert dispatch_headroom(ACCOUNTS, _records(personal=5, work=3), 5) == 0


# ---------------------------------------------------------------------------
# The schema: both keys optional, still bounded
# ---------------------------------------------------------------------------


class TestSchema:
    def test_package_defaults_set_neither_key(self) -> None:
        assert load_settings(None).concurrency == ConcurrencySettings(
            max_concurrency=None, initial_concurrency=None
        )

    @pytest.mark.parametrize("body", ["", "[concurrency]\n"], ids=["no-table", "empty-table"])
    def test_a_queue_toml_may_leave_both_keys_out(self, tmp_path: Path, body: str) -> None:
        toml = tmp_path / "claude_runner.toml"
        toml.write_text(body, encoding="utf-8")
        concurrency = load_settings(toml).concurrency
        assert concurrency.max_concurrency is None
        assert concurrency.initial_concurrency is None

    def test_explicit_values_load_as_written(self, tmp_path: Path) -> None:
        toml = tmp_path / "claude_runner.toml"
        toml.write_text(
            "[concurrency]\nmax_concurrency = 5\ninitial_concurrency = 2\n", encoding="utf-8"
        )
        concurrency = load_settings(toml).concurrency
        assert concurrency.max_concurrency == 5
        assert concurrency.initial_concurrency == 2

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("max_concurrency", "0"),
            ("max_concurrency", "-1"),
            ("initial_concurrency", "0"),
            ("initial_concurrency", "-3"),
            ("max_concurrency", '"five"'),
        ],
    )
    def test_invalid_values_still_fail_to_load(self, tmp_path: Path, key: str, value: str) -> None:
        toml = tmp_path / "claude_runner.toml"
        toml.write_text(f"[concurrency]\n{key} = {value}\n", encoding="utf-8")
        with pytest.raises(ConfigError, match=f"concurrency.{key}"):
            load_settings(toml)


# ---------------------------------------------------------------------------
# What a tick with nothing in flight may start
# ---------------------------------------------------------------------------


def _target(queue_dir: Path, settings: Settings) -> int:
    """The headroom ``tick_dispatch`` computes with nothing in flight, from
    the accounts resolved this call."""
    return dispatch_headroom(
        resolve_accounts(settings).accounts, [], orch_mod._target_concurrency(queue_dir, settings)
    )


def _seed_completed(queue_dir: Path) -> None:
    write_state_atomic(
        TaskState(task_id="warmup", status="completed"), state_path_for(queue_dir, "warmup")
    )


class TestTargetConcurrency:
    def test_omitted_key_is_no_queue_wide_cap(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        settings = queue_toml(tmp_path / "claude_runner.toml", two_accounts)
        assert orch_mod._target_concurrency(queue_dir, settings) is None
        assert _target(queue_dir, settings) == 8

    def test_follows_an_account_policy_change_on_the_next_evaluation(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        settings = queue_toml(tmp_path / "claude_runner.toml", two_accounts)
        assert _target(queue_dir, settings) == 8
        # Only the account's own file changes; the queue TOML and the loaded
        # Settings stay as they were.
        write_account_cap(two_accounts["work"], 4)
        assert _target(queue_dir, settings) == 9
        write_account_cap(two_accounts["personal"], 2)
        assert _target(queue_dir, settings) == 6

    def test_explicit_lower_value_caps_at_exactly_that_value(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        settings = queue_toml(tmp_path / "claude_runner.toml", two_accounts, "max_concurrency = 5")
        assert _target(queue_dir, settings) == 5

    def test_explicit_higher_value_does_not_bind(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        settings = queue_toml(tmp_path / "claude_runner.toml", two_accounts, "max_concurrency = 10")
        assert orch_mod._target_concurrency(queue_dir, settings) == 10
        assert _target(queue_dir, settings) == 8

    def test_omitted_initial_means_no_warm_up_and_no_completion_scan(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        settings = queue_toml(tmp_path / "claude_runner.toml", two_accounts)
        with patch.object(
            orch_mod, "_has_any_completed", side_effect=AssertionError("scanned state/")
        ):
            assert _target(queue_dir, settings) == 8

    def test_half_set_initial_only_caps_until_the_first_completion_then_the_sum(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        """Only ``initial_concurrency``: its cap until a first completion,
        then the accounts' sum, since no ``max_concurrency`` applies."""
        settings = queue_toml(
            tmp_path / "claude_runner.toml", two_accounts, "initial_concurrency = 3"
        )
        assert _target(queue_dir, settings) == 3
        _seed_completed(queue_dir)
        assert _target(queue_dir, settings) == 8

    def test_half_set_max_only_has_no_warm_up(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        """Only ``max_concurrency``: no warm-up cap, so its full value before
        any task completes."""
        settings = queue_toml(tmp_path / "claude_runner.toml", two_accounts, "max_concurrency = 5")
        assert _target(queue_dir, settings) == 5
        _seed_completed(queue_dir)
        assert _target(queue_dir, settings) == 5


class TestSingleAccount:
    """The legacy ``[claude].config_dir`` queue: one synthesised ``default`` account."""

    def _settings(self, tmp_path: Path, config_dir: str, concurrency: str = "") -> Settings:
        toml = tmp_path / "claude_runner.toml"
        body = f'[claude]\nconfig_dir = "{config_dir}"\n'
        if concurrency:
            body += f"[concurrency]\n{concurrency}\n"
        toml.write_text(body, encoding="utf-8")
        return load_settings(toml)

    def test_explicit_values_behave_as_before(self, tmp_path: Path, queue_dir: Path) -> None:
        write_account_cap(tmp_path / "acct", 4)
        settings = self._settings(
            tmp_path, str(tmp_path / "acct"), "max_concurrency = 3\ninitial_concurrency = 1"
        )
        assert [a.name for a in settings.accounts] == ["default"]
        assert _target(queue_dir, settings) == 1
        _seed_completed(queue_dir)
        assert _target(queue_dir, settings) == 3

    def test_omitted_is_the_one_accounts_cap(self, tmp_path: Path, queue_dir: Path) -> None:
        write_account_cap(tmp_path / "acct", 4)
        assert _target(queue_dir, self._settings(tmp_path, str(tmp_path / "acct"))) == 4

    def test_omitted_without_an_account_file_is_the_policy_default_of_1(
        self, tmp_path: Path, queue_dir: Path
    ) -> None:
        (tmp_path / "bare").mkdir()
        assert _target(queue_dir, self._settings(tmp_path, str(tmp_path / "bare"))) == 1
        # An empty config_dir has no runner-account.toml to read at all.
        assert _target(queue_dir, self._settings(tmp_path, "")) == 1


# ---------------------------------------------------------------------------
# End to end: tick_dispatch with real Settings and per-account files
# ---------------------------------------------------------------------------


class TestEndToEndTick:
    def test_eight_in_flight_across_caps_3_and_5_with_the_key_omitted(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        settings = queue_toml(tmp_path / "claude_runner.toml", two_accounts)
        make_tasks(queue_dir, 10)
        slots: dict[str, DispatchSlot] = {}
        with HeldDispatches() as held:
            dispatch_tick(queue_dir, settings, slots)
            assert len(slots) == 8
            assert in_flight_by_account(slots) == {"personal": 5, "work": 3}
            held.release(slots)

    def test_exactly_five_in_flight_with_an_explicit_5(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        settings = queue_toml(tmp_path / "claude_runner.toml", two_accounts, "max_concurrency = 5")
        make_tasks(queue_dir, 10)
        slots: dict[str, DispatchSlot] = {}
        with HeldDispatches() as held:
            dispatch_tick(queue_dir, settings, slots)
            # A second tick with the same in-flight set adds nothing.
            dispatch_tick(queue_dir, settings, slots)
            assert len(slots) == 5
            held.release(slots)

    def test_half_set_max_only_fills_to_it_on_the_first_tick(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        """No task has completed and no ``initial_concurrency`` is set, so no
        warm-up cap holds the first tick below 5."""
        settings = queue_toml(tmp_path / "claude_runner.toml", two_accounts, "max_concurrency = 5")
        make_tasks(queue_dir, 10)
        slots: dict[str, DispatchSlot] = {}
        with HeldDispatches() as held:
            dispatch_tick(queue_dir, settings, slots)
            assert len(slots) == 5
            held.release(slots)

    def test_explicit_higher_value_leaves_the_account_caps_in_charge(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        settings = queue_toml(tmp_path / "claude_runner.toml", two_accounts, "max_concurrency = 10")
        make_tasks(queue_dir, 12)
        slots: dict[str, DispatchSlot] = {}
        with HeldDispatches() as held:
            dispatch_tick(queue_dir, settings, slots)
            assert in_flight_by_account(slots) == {"personal": 5, "work": 3}
            held.release(slots)

    def test_a_raised_account_cap_admits_one_more_on_the_next_tick(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        settings = queue_toml(tmp_path / "claude_runner.toml", two_accounts)
        make_tasks(queue_dir, 10)
        slots: dict[str, DispatchSlot] = {}
        with HeldDispatches() as held:
            dispatch_tick(queue_dir, settings, slots)
            assert len(slots) == 8
            write_account_cap(two_accounts["work"], 4)
            dispatch_tick(queue_dir, settings, slots)
            assert len(slots) == 9
            assert in_flight_by_account(slots) == {"personal": 5, "work": 4}
            held.release(slots)

    def test_a_lowered_cap_does_not_hold_back_the_other_account(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        """personal runs 5 when its cap drops to 2: the accounts' caps now sum
        to 5 with 5 in flight, but work's 3 free slots still fill."""
        settings = queue_toml(tmp_path / "claude_runner.toml", two_accounts)
        make_tasks(queue_dir, 6)
        stop = threading.Event()
        slots = busy_slots(stop, personal=5)
        write_account_cap(two_accounts["personal"], 2)
        try:
            with HeldDispatches() as held:
                dispatch_tick(queue_dir, settings, slots)
                assert dispatched_by_account(slots) == {"work": 3}
                stop.set()
                held.release(slots)
        finally:
            stop.set()

    @pytest.mark.parametrize(
        ("concurrency", "new"),
        [("", {"personal": 5, "work": 3}), ("max_concurrency = 10", {"personal": 5, "work": 1})],
        ids=["omitted", "explicit-10"],
    )
    def test_an_undeclared_accounts_tasks_count_only_against_an_explicit_cap(
        self,
        tmp_path: Path,
        queue_dir: Path,
        two_accounts: dict[str, Path],
        concurrency: str,
        new: dict[str, int],
    ) -> None:
        """4 tasks still run on an account taken out of ``[[accounts]]``."""
        settings = queue_toml(tmp_path / "claude_runner.toml", two_accounts, concurrency)
        make_tasks(queue_dir, 10)
        stop = threading.Event()
        slots = busy_slots(stop, retired=4)
        try:
            with HeldDispatches() as held:
                dispatch_tick(queue_dir, settings, slots)
                assert dispatched_by_account(slots) == new
                stop.set()
                held.release(slots)
        finally:
            stop.set()

    def test_a_slowed_account_gets_exactly_its_throttle_target(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        """The headroom counts each account's max_concurrency (8 here) as an
        upper bound; choose_account holds personal, SLOWING_DOWN at a target
        of 2, to exactly 2."""
        settings = queue_toml(tmp_path / "claude_runner.toml", two_accounts)
        make_tasks(queue_dir, 10)
        since = datetime(2026, 10, 15, tzinfo=UTC)
        snapshot = SupervisorSnapshot(
            state=SupervisorState.DISPATCHING,
            since=since,
            accounts={
                "personal": AccountState(
                    state=SupervisorState.SLOWING_DOWN, since=since, target_concurrency=2
                ),
                "work": AccountState(state=SupervisorState.DISPATCHING, since=since),
            },
        )
        assert dispatch_headroom(resolve_accounts(settings).accounts, [], None) == 8
        slots: dict[str, DispatchSlot] = {}
        with HeldDispatches() as held:
            orch_mod.tick_dispatch(
                queue_dir=queue_dir,
                settings=settings,
                clock=RealClock(),
                snapshot=snapshot,
                in_flight_slots=slots,
            )
            assert in_flight_by_account(slots) == {"personal": 2, "work": 3}
            held.release(slots)

    def test_single_account_with_explicit_cap_is_unchanged(
        self, tmp_path: Path, queue_dir: Path
    ) -> None:
        write_account_cap(tmp_path / "acct", 4)
        toml = tmp_path / "claude_runner.toml"
        toml.write_text(
            f'[claude]\nconfig_dir = "{tmp_path / "acct"}"\n[concurrency]\nmax_concurrency = 3\n',
            encoding="utf-8",
        )
        settings = load_settings(toml)
        make_tasks(queue_dir, 6)
        slots: dict[str, DispatchSlot] = {}
        with HeldDispatches() as held:
            dispatch_tick(queue_dir, settings, slots)
            assert in_flight_by_account(slots) == {"default": 3}
            held.release(slots)


# ---------------------------------------------------------------------------
# QueueCeilingWatch: the INFO line and the binding warning
# ---------------------------------------------------------------------------


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == DAEMON_LOGGER
        and r.levelno == logging.WARNING
        and "[concurrency].max_concurrency" in r.getMessage()
    ]


def _ceiling_infos(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == DAEMON_LOGGER
        and r.levelno == logging.INFO
        and r.getMessage().startswith("concurrency ceiling: ")
    ]


BINDING_WARNING = (
    "[concurrency].max_concurrency = 5 holds the queue below its accounts' own caps, "
    "which sum to 8 (personal 5 + work 3): at most 5 tasks run at once. Delete the key "
    "to make each account's max_concurrency the only limit, or keep it as a deliberate "
    "global cap"
)


class TestQueueCeilingWatch:
    def test_a_binding_ceiling_warns_exactly_once(self, caplog: pytest.LogCaptureFixture) -> None:
        watch = QueueCeilingWatch()
        with caplog.at_level(logging.WARNING, logger=DAEMON_LOGGER):
            for _ in range(5):
                watch.observe(QueueCeiling(configured=5, account_caps=CAPS))
        assert _warnings(caplog) == [BINDING_WARNING]

    @pytest.mark.parametrize("configured", [None, 8, 10])
    def test_a_ceiling_that_does_not_bind_never_warns(
        self, caplog: pytest.LogCaptureFixture, configured: int | None
    ) -> None:
        watch = QueueCeilingWatch()
        with caplog.at_level(logging.WARNING, logger=DAEMON_LOGGER):
            for _ in range(3):
                watch.observe(QueueCeiling(configured=configured, account_caps=CAPS))
        assert _warnings(caplog) == []

    def test_the_ceiling_is_logged_once_and_again_after_rearm(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        watch = QueueCeilingWatch()
        unbound = QueueCeiling(configured=None, account_caps=CAPS)
        with caplog.at_level(logging.INFO, logger=DAEMON_LOGGER):
            watch.observe(unbound)
            watch.observe(unbound)
            watch.rearm()
            watch.observe(unbound)
            watch.observe(unbound)
        assert (
            _ceiling_infos(caplog)
            == [
                "concurrency ceiling: 8, the sum of the accounts' max_concurrency "
                "(personal 5 + work 3)"
            ]
            * 2
        )

    def test_rearm_warns_again_for_the_same_numbers(self, caplog: pytest.LogCaptureFixture) -> None:
        watch = QueueCeilingWatch()
        ceiling = QueueCeiling(configured=5, account_caps=CAPS)
        with caplog.at_level(logging.WARNING, logger=DAEMON_LOGGER):
            watch.observe(ceiling)
            watch.observe(ceiling)
            watch.rearm()
            watch.observe(ceiling)
            watch.observe(ceiling)
        assert _warnings(caplog) == [BINDING_WARNING, BINDING_WARNING]

    def test_an_account_policy_change_that_makes_it_bind_warns_once(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        watch = QueueCeilingWatch()
        with caplog.at_level(logging.WARNING, logger=DAEMON_LOGGER):
            watch.observe(QueueCeiling(configured=5, account_caps={"personal": 2, "work": 3}))
            assert _warnings(caplog) == []
            for _ in range(3):
                watch.observe(QueueCeiling(configured=5, account_caps=CAPS))
        assert _warnings(caplog) == [BINDING_WARNING]

    def test_a_change_of_either_number_while_binding_warns_again(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        watch = QueueCeilingWatch()
        with caplog.at_level(logging.WARNING, logger=DAEMON_LOGGER):
            watch.observe(QueueCeiling(configured=5, account_caps=CAPS))
            watch.observe(QueueCeiling(configured=5, account_caps={"personal": 5, "work": 4}))
            watch.observe(QueueCeiling(configured=6, account_caps={"personal": 5, "work": 4}))
        found = _warnings(caplog)
        assert len(found) == 3
        assert "sum to 9 (personal 5 + work 4)" in found[1]
        assert found[2].startswith("[concurrency].max_concurrency = 6 ")

    def test_binding_again_after_a_spell_unbound_warns_again(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        watch = QueueCeilingWatch()
        binding = QueueCeiling(configured=5, account_caps=CAPS)
        with caplog.at_level(logging.WARNING, logger=DAEMON_LOGGER):
            watch.observe(binding)
            watch.observe(QueueCeiling(configured=None, account_caps=CAPS))
            watch.observe(binding)
        assert _warnings(caplog) == [BINDING_WARNING, BINDING_WARNING]


# ---------------------------------------------------------------------------
# The INFO line and the warning as the supervisor logs them
# ---------------------------------------------------------------------------


SINGLE_ACCOUNT_WARNING = (
    "[concurrency].max_concurrency = 2 holds the queue below its accounts' own caps, "
    "which sum to 3 (default 3): at most 2 tasks run at once. Delete the key to make "
    "each account's max_concurrency the only limit, or keep it as a deliberate global cap"
)
BINDING_INFO = (
    "concurrency ceiling: 2 from [concurrency].max_concurrency, which binds: "
    "the accounts' max_concurrency sum to 3 (default 3)"
)
SUM_INFO = "concurrency ceiling: 3, the sum of the accounts' max_concurrency (default 3)"


@pytest.mark.usefixtures("private_global_lock")
class TestSupervisorLog:
    def test_warns_once_and_logs_the_ceiling_once_over_several_ticks(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        qd, config = single_account_queue(tmp_path, "max_concurrency = 2")
        with caplog.at_level(logging.INFO, logger=DAEMON_LOGGER):
            run_daemon(qd, config, pending_count_fn=lambda: 0, install_signal_handlers=False)
        assert _warnings(caplog) == [SINGLE_ACCOUNT_WARNING]
        assert _ceiling_infos(caplog) == [BINDING_INFO]

    @pytest.mark.parametrize(
        ("concurrency", "info"),
        [
            ("", SUM_INFO),
            ("max_concurrency = 3", SUM_INFO + "; [concurrency].max_concurrency = 3 does not bind"),
            ("max_concurrency = 4", SUM_INFO + "; [concurrency].max_concurrency = 4 does not bind"),
        ],
        ids=["omitted", "equal", "higher"],
    )
    def test_a_ceiling_that_does_not_bind_is_logged_at_info_only(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, concurrency: str, info: str
    ) -> None:
        qd, config = single_account_queue(tmp_path, concurrency)
        with caplog.at_level(logging.INFO, logger=DAEMON_LOGGER):
            run_daemon(qd, config, pending_count_fn=lambda: 0, install_signal_handlers=False)
        assert _warnings(caplog) == []
        assert _ceiling_infos(caplog) == [info]

    @pytest.mark.allow_self_signal
    def test_a_reload_logs_both_once_more(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        qd, config = single_account_queue(tmp_path, "max_concurrency = 2")
        with caplog.at_level(logging.INFO, logger=DAEMON_LOGGER):
            run_daemon(
                qd,
                config,
                pending_count_fn=sighup_in_second_tick(lambda: None),
                install_signal_handlers=True,
            )
        assert any("SIGHUP received: reloaded config" in r.getMessage() for r in caplog.records)
        assert _warnings(caplog) == [SINGLE_ACCOUNT_WARNING] * 2
        assert _ceiling_infos(caplog) == [BINDING_INFO] * 2

    @pytest.mark.allow_self_signal
    def test_a_failed_reload_logs_neither_again(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        qd, config = single_account_queue(tmp_path, "max_concurrency = 2")

        def break_config() -> None:
            config.write_text("this is not [valid toml", encoding="utf-8")

        with caplog.at_level(logging.INFO, logger=DAEMON_LOGGER):
            run_daemon(
                qd,
                config,
                pending_count_fn=sighup_in_second_tick(break_config),
                install_signal_handlers=True,
            )
        assert any("reload failed" in r.getMessage() for r in caplog.records)
        assert _warnings(caplog) == [SINGLE_ACCOUNT_WARNING]
        assert _ceiling_infos(caplog) == [BINDING_INFO]

    @pytest.mark.allow_self_signal
    def test_removing_the_key_on_reload_stops_the_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        qd, config = single_account_queue(tmp_path, "max_concurrency = 2")

        def drop_the_key() -> None:
            config.write_text(single_account_body(tmp_path, ""), encoding="utf-8")

        with caplog.at_level(logging.INFO, logger=DAEMON_LOGGER):
            run_daemon(
                qd,
                config,
                pending_count_fn=sighup_in_second_tick(drop_the_key),
                install_signal_handlers=True,
            )
        assert any("SIGHUP received: reloaded config" in r.getMessage() for r in caplog.records)
        assert _warnings(caplog) == [SINGLE_ACCOUNT_WARNING]
        assert _ceiling_infos(caplog) == [BINDING_INFO, SUM_INFO]


# ---------------------------------------------------------------------------
# Force-dispatch waits for a free slot
# ---------------------------------------------------------------------------


class TestForceDispatch:
    @pytest.mark.parametrize(
        ("concurrency", "busy", "dispatched"),
        [
            ("", {"personal": 5, "work": 2}, 1),
            ("", {"personal": 5, "work": 3}, 0),
            # An account over its own cap leaves the other's slots free.
            ("", {"personal": 8}, 1),
            # An explicit cap counts every in-flight task.
            ("max_concurrency = 5", {"personal": 4}, 1),
            ("max_concurrency = 5", {"personal": 5}, 0),
        ],
        ids=["omitted-free", "omitted-full", "omitted-over-cap", "explicit-4", "explicit-5"],
    )
    def test_a_request_waits_only_without_a_free_slot(
        self,
        tmp_path: Path,
        queue_dir: Path,
        two_accounts: dict[str, Path],
        concurrency: str,
        busy: dict[str, int],
        dispatched: int,
    ) -> None:
        settings = queue_toml(tmp_path / "claude_runner.toml", two_accounts, concurrency)
        make_tasks(queue_dir, 1)
        fd_mod.write_request(queue_dir, "t00")
        stop = threading.Event()
        slots = busy_slots(stop, **busy)
        try:
            with HeldDispatches() as held:
                n = fd_mod.tick_consume(
                    queue_dir=queue_dir, settings=settings, clock=RealClock(), in_flight_slots=slots
                )
                assert n == dispatched
                assert fd_mod.request_path(queue_dir, "t00").exists() is (dispatched == 0)
                held.release({k: v for k, v in slots.items() if k == "t00"})
        finally:
            stop.set()

    @pytest.mark.parametrize(
        ("concurrency", "busy", "parked", "dispatched"),
        [
            ("", {"personal": 5, "work": 2}, {}, 1),
            ("", {"personal": 5, "work": 2}, {"work": 1}, 0),
            ("max_concurrency = 5", {"personal": 4}, {}, 1),
            ("max_concurrency = 5", {"personal": 4}, {"work": 1}, 0),
        ],
        ids=["omitted-free", "omitted-parked-fills", "explicit-free", "explicit-parked-fills"],
    )
    def test_parked_workers_take_their_slots(
        self,
        tmp_path: Path,
        queue_dir: Path,
        two_accounts: dict[str, Path],
        concurrency: str,
        busy: dict[str, int],
        parked: dict[str, int],
        dispatched: int,
    ) -> None:
        """The last tick's in-flight list holds every slot's record and
        each parked live worker's. The parked workers count against their
        accounts as the tick counts them; the slots count once."""
        settings = queue_toml(tmp_path / "claude_runner.toml", two_accounts, concurrency)
        make_tasks(queue_dir, 1)
        fd_mod.write_request(queue_dir, "t00")
        stop = threading.Event()
        slots = busy_slots(stop, **busy)
        prior = [*to_in_flight_records(slots), *_parked_records(**parked)]
        try:
            with HeldDispatches() as held:
                n = fd_mod.tick_consume(
                    queue_dir=queue_dir,
                    settings=settings,
                    clock=RealClock(),
                    in_flight_slots=slots,
                    prior_in_flight=prior,
                )
                assert n == dispatched
                assert fd_mod.request_path(queue_dir, "t00").exists() is (dispatched == 0)
                held.release({k: v for k, v in slots.items() if k == "t00"})
        finally:
            stop.set()


def _parked_records(**by_account: int) -> list[InFlightRecord]:
    """In-flight records of parked live workers, ``n`` per account: no slot
    in this supervisor, as a tick's tracker holds them."""
    return [
        InFlightRecord(
            task_id=f"parked-{account}-{i}",
            account=account,
            started_at=datetime(2026, 10, 15, tzinfo=UTC),
        )
        for account, n in by_account.items()
        for i in range(n)
    ]


class TestForceDispatchAccount:
    @pytest.mark.parametrize(
        ("busy", "picked"),
        [
            ({}, "personal"),
            ({"personal": 5, "work": 2}, "work"),
            ({"personal": 4, "work": 0}, "personal"),
        ],
        ids=["both-free-first-wins", "first-full-goes-to-second", "first-has-room"],
    )
    def test_an_unbound_request_goes_to_the_first_account_with_a_free_slot(
        self,
        tmp_path: Path,
        queue_dir: Path,
        two_accounts: dict[str, Path],
        busy: dict[str, int],
        picked: str,
    ) -> None:
        settings = queue_toml(tmp_path / "claude_runner.toml", two_accounts)
        make_tasks(queue_dir, 1)
        fd_mod.write_request(queue_dir, "t00")
        stop = threading.Event()
        slots = busy_slots(stop, **busy)
        try:
            with HeldDispatches() as held:
                n = fd_mod.tick_consume(
                    queue_dir=queue_dir, settings=settings, clock=RealClock(), in_flight_slots=slots
                )
                assert n == 1
                assert slots["t00"].account == picked
                stop.set()
                held.release(slots)
        finally:
            stop.set()

    def test_a_parked_worker_fills_the_first_account(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        """personal has 4 of its 5 slots busy and a parked worker in the
        fifth: the request goes to work."""
        settings = queue_toml(tmp_path / "claude_runner.toml", two_accounts)
        make_tasks(queue_dir, 1)
        fd_mod.write_request(queue_dir, "t00")
        stop = threading.Event()
        slots = busy_slots(stop, personal=4)
        prior = [*to_in_flight_records(slots), *_parked_records(personal=1)]
        try:
            with HeldDispatches() as held:
                n = fd_mod.tick_consume(
                    queue_dir=queue_dir,
                    settings=settings,
                    clock=RealClock(),
                    in_flight_slots=slots,
                    prior_in_flight=prior,
                )
                assert n == 1
                assert slots["t00"].account == "work"
                stop.set()
                held.release(slots)
        finally:
            stop.set()


# ---------------------------------------------------------------------------
# What the CLI shows
# ---------------------------------------------------------------------------


class TestCli:
    def test_account_list_json_carries_the_ceiling(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        config = tmp_path / "claude_runner.toml"
        queue_toml(config, two_accounts, "max_concurrency = 5")
        result = invoke_cli(
            ["account", "list", "--config", str(config), "--queue", str(queue_dir), "--json"]
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["queue_ceiling"] == {
            "ceiling": 5,
            "max_concurrency": 5,
            "accounts_total": 8,
            "account_caps": {"personal": 5, "work": 3},
            "binds": True,
            "broken_accounts": [],
            "initial_concurrency": None,
        }

    @pytest.mark.parametrize("columns", [80, 1000])
    def test_account_list_prints_the_ceiling_on_one_line(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path], columns: int
    ) -> None:
        config = tmp_path / "claude_runner.toml"
        queue_toml(config, two_accounts, "initial_concurrency = 2")
        result = invoke_cli(
            ["account", "list", "--config", str(config), "--queue", str(queue_dir)], columns
        )
        assert result.exit_code == 0, result.output
        assert (
            "queue ceiling: 8, the sum of the accounts' max_concurrency (personal 5 + work 3); "
            "initial_concurrency 2 until a first task completes\n"
        ) in result.stdout

    @pytest.mark.parametrize("columns", [80, 1000])
    def test_supervisor_status_prints_the_ceiling_on_one_line(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path], columns: int
    ) -> None:
        queue_toml(queue_dir / "claude_runner.toml", two_accounts, "max_concurrency = 5")
        text = invoke_cli(["supervisor", "status", "--queue", str(queue_dir)], columns)
        assert text.exit_code == 0, text.output
        assert (
            "Queue ceiling:    5 from [concurrency].max_concurrency, which binds: the "
            "accounts' max_concurrency sum to 8 (personal 5 + work 3)\n"
        ) in text.stdout

    def test_supervisor_status_json_carries_the_ceiling(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        queue_toml(queue_dir / "claude_runner.toml", two_accounts, "max_concurrency = 10")
        result = invoke_cli(["supervisor", "status", "--queue", str(queue_dir), "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["queue_ceiling"] == {
            "ceiling": 8,
            "max_concurrency": 10,
            "accounts_total": 8,
            "account_caps": {"personal": 5, "work": 3},
            "binds": False,
            "broken_accounts": [],
            "initial_concurrency": None,
        }
