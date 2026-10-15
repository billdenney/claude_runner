"""The queue-wide concurrency ceiling (ADR-0035).

Each account's ``max_concurrency``, in its own ``runner-account.toml``, caps
the tasks that run through it. The queue TOML's ``[concurrency]`` keys are
optional. With ``max_concurrency`` left out, the ceiling on the queue's
in-flight tasks is the sum of the accounts' caps, read on the same tick as
dispatch, so it never holds an account below its own cap. An explicit value
is still a global cap; one below that sum binds, and the supervisor warns
once per config load and once per change of either number.

The numbers are the live queue's: ``work`` capped at 3 and ``personal`` at 5,
where a queue-wide 5 held the total at 5 and gave ``personal`` 2 slots.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import threading
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from claude_task_runner.cli import app
from claude_task_runner.clock import RealClock
from claude_task_runner.config.loader import (
    PER_ACCOUNT_TOML_NAME,
    ConfigError,
    load_settings,
    resolve_accounts,
)
from claude_task_runner.config.schema import (
    AccountConcurrencyPolicy,
    AccountPolicy,
    ConcurrencySettings,
    ResolvedAccount,
    Settings,
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
from claude_task_runner.runner import orchestrator as orch_mod
from claude_task_runner.runner.account_dispatch import QueueCeiling
from claude_task_runner.runner.in_flight import DispatchSlot
from claude_task_runner.supervisor.daemon import QueueCeilingWarning, start_daemon
from claude_task_runner.supervisor.states import (
    AccountState,
    SupervisorSnapshot,
    SupervisorState,
)
from claude_task_runner.usage.models import UsageReading, WindowReading
from claude_task_runner.usage.source import FakeUsageSource

DAEMON_LOGGER = "claude_task_runner.supervisor.daemon"

# ---------------------------------------------------------------------------
# Fixtures: a queue whose accounts' caps come from real runner-account.toml
# files, loaded the way the supervisor loads them.
# ---------------------------------------------------------------------------


def _write_account_cap(config_dir: Path, cap: int) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / PER_ACCOUNT_TOML_NAME).write_text(
        f"[concurrency]\nmax_concurrency = {cap}\n", encoding="utf-8"
    )


def _queue_toml(path: Path, accounts: dict[str, Path], concurrency: str = "") -> Settings:
    """Write and load a ``claude_runner.toml`` declaring ``accounts``.

    ``concurrency`` is the body of a ``[concurrency]`` table; empty leaves the
    table out, as an operator who relies on the per-account caps would.
    """
    lines: list[str] = []
    for name, config_dir in accounts.items():
        lines += ["[[accounts]]", f'name = "{name}"', f'config_dir = "{config_dir}"', ""]
    if concurrency:
        lines += ["[concurrency]", concurrency, ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return load_settings(path)


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
    _write_account_cap(dirs["personal"], 5)
    _write_account_cap(dirs["work"], 3)
    return dirs


def _resolved(name: str, cap: int) -> ResolvedAccount:
    return ResolvedAccount(
        name=name,
        config_dir="",
        policy=AccountPolicy(concurrency=AccountConcurrencyPolicy(max_concurrency=cap)),
    )


# ---------------------------------------------------------------------------
# QueueCeiling: the arithmetic
# ---------------------------------------------------------------------------

CAPS = {"personal": 5, "work": 3}
"""The live queue's accounts' own ``max_concurrency``."""


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
        assert ceiling.value == configured
        assert ceiling.binds is False

    def test_of_reads_each_resolved_accounts_own_cap(self) -> None:
        ceiling = QueueCeiling.of(None, [_resolved("personal", 5), _resolved("work", 3)])
        assert ceiling == QueueCeiling(configured=None, account_caps=CAPS)

    def test_one_account_is_that_accounts_cap(self) -> None:
        assert QueueCeiling.of(None, [_resolved("default", 1)]).value == 1
        assert QueueCeiling.of(None, [_resolved("default", 4)]).value == 4

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
                "10 from [concurrency].max_concurrency, which does not bind: the "
                "accounts' max_concurrency sum to 8 (personal 5 + work 3)",
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
        }


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
# _target_concurrency: the number tick_dispatch fills up to
# ---------------------------------------------------------------------------


def _target(queue_dir: Path, settings: Settings) -> int:
    """The orchestrator's ceiling, from the accounts resolved this call."""
    return orch_mod._target_concurrency(queue_dir, settings, resolve_accounts(settings))


def _seed_completed(queue_dir: Path) -> None:
    write_state_atomic(
        TaskState(task_id="warmup", status="completed"), state_path_for(queue_dir, "warmup")
    )


class TestTargetConcurrency:
    def test_omitted_key_is_the_sum_of_the_account_caps(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        settings = _queue_toml(tmp_path / "claude_runner.toml", two_accounts)
        assert _target(queue_dir, settings) == 8

    def test_follows_an_account_policy_change_on_the_next_evaluation(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        settings = _queue_toml(tmp_path / "claude_runner.toml", two_accounts)
        assert _target(queue_dir, settings) == 8
        # Only the account's own file changes; the queue TOML and the loaded
        # Settings stay as they were.
        _write_account_cap(two_accounts["work"], 4)
        assert _target(queue_dir, settings) == 9
        _write_account_cap(two_accounts["personal"], 2)
        assert _target(queue_dir, settings) == 6

    def test_explicit_lower_value_caps_at_exactly_that_value(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        settings = _queue_toml(tmp_path / "claude_runner.toml", two_accounts, "max_concurrency = 5")
        assert _target(queue_dir, settings) == 5

    def test_explicit_higher_value_does_not_bind(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        settings = _queue_toml(
            tmp_path / "claude_runner.toml", two_accounts, "max_concurrency = 10"
        )
        assert _target(queue_dir, settings) == 10
        ceiling = QueueCeiling.of(10, resolve_accounts(settings))
        assert ceiling.binds is False

    def test_omitted_initial_means_no_warm_up_and_no_completion_scan(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        settings = _queue_toml(tmp_path / "claude_runner.toml", two_accounts)
        with patch.object(
            orch_mod, "_has_any_completed", side_effect=AssertionError("scanned state/")
        ):
            assert _target(queue_dir, settings) == 8

    def test_explicit_initial_caps_until_the_first_completion(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        settings = _queue_toml(
            tmp_path / "claude_runner.toml", two_accounts, "initial_concurrency = 2"
        )
        assert _target(queue_dir, settings) == 2
        _seed_completed(queue_dir)
        assert _target(queue_dir, settings) == 8


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
        _write_account_cap(tmp_path / "acct", 4)
        settings = self._settings(
            tmp_path, str(tmp_path / "acct"), "max_concurrency = 3\ninitial_concurrency = 1"
        )
        assert [a.name for a in settings.accounts] == ["default"]
        assert _target(queue_dir, settings) == 1
        _seed_completed(queue_dir)
        assert _target(queue_dir, settings) == 3

    def test_omitted_is_the_one_accounts_cap(self, tmp_path: Path, queue_dir: Path) -> None:
        _write_account_cap(tmp_path / "acct", 4)
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


def _make_tasks(queue_dir: Path, n: int) -> None:
    for i in range(n):
        task = Task.model_validate({"id": f"t{i:02d}", "title": f"Task {i}", "prompt": "do it"})
        write_task_atomic(task, task_path_for(queue_dir, task.id))


def _dispatching_snapshot(names: list[str]) -> SupervisorSnapshot:
    since = datetime(2026, 10, 15, tzinfo=UTC)
    return SupervisorSnapshot(
        state=SupervisorState.DISPATCHING,
        since=since,
        accounts={
            name: AccountState(state=SupervisorState.DISPATCHING, since=since) for name in names
        },
    )


class _HeldDispatches:
    """Patches the dispatcher so every dispatched task stays in flight until
    :meth:`release`, the way a real worker does for minutes."""

    def __init__(self) -> None:
        self._gate = threading.Event()
        self._patch = patch(
            "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
            side_effect=lambda **_kw: self._gate.wait(timeout=30),
        )

    def __enter__(self) -> _HeldDispatches:
        self._patch.start()
        return self

    def release(self, slots: dict[str, DispatchSlot]) -> None:
        self._gate.set()
        for slot in slots.values():
            slot.thread.join(timeout=5)

    def __exit__(self, *_exc: object) -> None:
        self._gate.set()
        self._patch.stop()


def _tick(queue_dir: Path, settings: Settings, slots: dict[str, DispatchSlot]) -> None:
    """One dispatch pass. ``accounts`` is left to tick_dispatch, which
    resolves them from the files, as it does under the supervisor."""
    orch_mod.tick_dispatch(
        queue_dir=queue_dir,
        settings=settings,
        clock=RealClock(),
        snapshot=_dispatching_snapshot([a.name for a in settings.accounts]),
        in_flight_slots=slots,
    )


def _by_account(slots: dict[str, DispatchSlot]) -> dict[str, int]:
    return dict(Counter(slot.account for slot in slots.values()))


class TestEndToEndTick:
    def test_eight_in_flight_across_caps_3_and_5_with_the_key_omitted(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        settings = _queue_toml(tmp_path / "claude_runner.toml", two_accounts)
        _make_tasks(queue_dir, 10)
        slots: dict[str, DispatchSlot] = {}
        with _HeldDispatches() as held:
            _tick(queue_dir, settings, slots)
            assert len(slots) == 8
            assert _by_account(slots) == {"personal": 5, "work": 3}
            held.release(slots)

    def test_exactly_five_in_flight_with_an_explicit_5(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        settings = _queue_toml(tmp_path / "claude_runner.toml", two_accounts, "max_concurrency = 5")
        _make_tasks(queue_dir, 10)
        slots: dict[str, DispatchSlot] = {}
        with _HeldDispatches() as held:
            _tick(queue_dir, settings, slots)
            # A second tick with the same in-flight set adds nothing.
            _tick(queue_dir, settings, slots)
            assert len(slots) == 5
            held.release(slots)

    def test_explicit_higher_value_leaves_the_account_caps_in_charge(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        settings = _queue_toml(
            tmp_path / "claude_runner.toml", two_accounts, "max_concurrency = 10"
        )
        _make_tasks(queue_dir, 12)
        slots: dict[str, DispatchSlot] = {}
        with _HeldDispatches() as held:
            _tick(queue_dir, settings, slots)
            assert _by_account(slots) == {"personal": 5, "work": 3}
            held.release(slots)

    def test_a_raised_account_cap_admits_one_more_on_the_next_tick(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        settings = _queue_toml(tmp_path / "claude_runner.toml", two_accounts)
        _make_tasks(queue_dir, 10)
        slots: dict[str, DispatchSlot] = {}
        with _HeldDispatches() as held:
            _tick(queue_dir, settings, slots)
            assert len(slots) == 8
            _write_account_cap(two_accounts["work"], 4)
            _tick(queue_dir, settings, slots)
            assert len(slots) == 9
            assert _by_account(slots) == {"personal": 5, "work": 4}
            held.release(slots)

    def test_single_account_with_explicit_cap_is_unchanged(
        self, tmp_path: Path, queue_dir: Path
    ) -> None:
        _write_account_cap(tmp_path / "acct", 4)
        toml = tmp_path / "claude_runner.toml"
        toml.write_text(
            f'[claude]\nconfig_dir = "{tmp_path / "acct"}"\n[concurrency]\nmax_concurrency = 3\n',
            encoding="utf-8",
        )
        settings = load_settings(toml)
        _make_tasks(queue_dir, 6)
        slots: dict[str, DispatchSlot] = {}
        with _HeldDispatches() as held:
            _tick(queue_dir, settings, slots)
            assert _by_account(slots) == {"default": 3}
            held.release(slots)


# ---------------------------------------------------------------------------
# The binding warning
# ---------------------------------------------------------------------------


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == DAEMON_LOGGER
        and r.levelno == logging.WARNING
        and "[concurrency].max_concurrency" in r.getMessage()
    ]


BINDING_WARNING = (
    "[concurrency].max_concurrency = 5 holds the queue below its accounts' own caps, "
    "which sum to 8 (personal 5 + work 3): at most 5 tasks run at once. Delete the key "
    "to make each account's max_concurrency the only limit, or keep it as a deliberate "
    "global cap"
)


class TestQueueCeilingWarning:
    def test_a_binding_ceiling_warns_exactly_once(self, caplog: pytest.LogCaptureFixture) -> None:
        warning = QueueCeilingWarning()
        with caplog.at_level(logging.WARNING, logger=DAEMON_LOGGER):
            for _ in range(5):
                warning.observe(QueueCeiling(configured=5, account_caps=CAPS))
        assert _warnings(caplog) == [BINDING_WARNING]

    @pytest.mark.parametrize("configured", [None, 8, 10])
    def test_a_ceiling_that_does_not_bind_never_warns(
        self, caplog: pytest.LogCaptureFixture, configured: int | None
    ) -> None:
        warning = QueueCeilingWarning()
        with caplog.at_level(logging.WARNING, logger=DAEMON_LOGGER):
            for _ in range(3):
                warning.observe(QueueCeiling(configured=configured, account_caps=CAPS))
        assert _warnings(caplog) == []

    def test_rearm_warns_again_for_the_same_numbers(self, caplog: pytest.LogCaptureFixture) -> None:
        warning = QueueCeilingWarning()
        ceiling = QueueCeiling(configured=5, account_caps=CAPS)
        with caplog.at_level(logging.WARNING, logger=DAEMON_LOGGER):
            warning.observe(ceiling)
            warning.observe(ceiling)
            warning.rearm()
            warning.observe(ceiling)
            warning.observe(ceiling)
        assert _warnings(caplog) == [BINDING_WARNING, BINDING_WARNING]

    def test_an_account_policy_change_that_makes_it_bind_warns_once(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        warning = QueueCeilingWarning()
        with caplog.at_level(logging.WARNING, logger=DAEMON_LOGGER):
            warning.observe(QueueCeiling(configured=5, account_caps={"personal": 2, "work": 3}))
            assert _warnings(caplog) == []
            for _ in range(3):
                warning.observe(QueueCeiling(configured=5, account_caps=CAPS))
        assert _warnings(caplog) == [BINDING_WARNING]

    def test_a_change_of_either_number_while_binding_warns_again(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        warning = QueueCeilingWarning()
        with caplog.at_level(logging.WARNING, logger=DAEMON_LOGGER):
            warning.observe(QueueCeiling(configured=5, account_caps=CAPS))
            warning.observe(QueueCeiling(configured=5, account_caps={"personal": 5, "work": 4}))
            warning.observe(QueueCeiling(configured=6, account_caps={"personal": 5, "work": 4}))
        found = _warnings(caplog)
        assert len(found) == 3
        assert "sum to 9 (personal 5 + work 4)" in found[1]
        assert found[2].startswith("[concurrency].max_concurrency = 6 ")

    def test_binding_again_after_a_spell_unbound_warns_again(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        warning = QueueCeilingWarning()
        binding = QueueCeiling(configured=5, account_caps=CAPS)
        with caplog.at_level(logging.WARNING, logger=DAEMON_LOGGER):
            warning.observe(binding)
            warning.observe(QueueCeiling(configured=None, account_caps=CAPS))
            warning.observe(binding)
        assert _warnings(caplog) == [BINDING_WARNING, BINDING_WARNING]


# ---------------------------------------------------------------------------
# The warning as the supervisor raises it
# ---------------------------------------------------------------------------


def _reading() -> UsageReading:
    return UsageReading(
        captured_at=datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC),
        five_hour=WindowReading(
            utilization_pct=10,
            resets_at_raw="x",
            resets_at=datetime(2026, 10, 15, 17, 0, 0, tzinfo=UTC),
        ),
        seven_day=WindowReading(
            utilization_pct=10,
            resets_at_raw="x",
            resets_at=datetime(2026, 10, 20, 11, 0, 0, tzinfo=UTC),
        ),
    )


def _single_account_queue(tmp_path: Path, concurrency: str) -> tuple[Path, Path]:
    """A one-account queue whose account is capped at 3. Returns
    ``(queue_dir, config_path)``."""
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    _write_account_cap(tmp_path / "acct", 3)
    config = tmp_path / "claude_runner.toml"
    body = f'[claude]\nconfig_dir = "{tmp_path / "acct"}"\n[usage]\npoll_interval_s = 0.01\n'
    if concurrency:
        body += f"[concurrency]\n{concurrency}\n"
    config.write_text(body, encoding="utf-8")
    return qd, config


def _run_daemon(
    qd: Path, config: Path, *, pending_count_fn: Any, install_signal_handlers: bool
) -> None:
    start_daemon(
        queue_dir=qd,
        settings=load_settings(config),
        source=FakeUsageSource([_reading()]),
        pending_count_fn=pending_count_fn,
        in_flight_count_fn=lambda: 0,
        install_signal_handlers=install_signal_handlers,
        max_ticks=4,
        config_path=config,
    )


SINGLE_ACCOUNT_WARNING = (
    "[concurrency].max_concurrency = 2 holds the queue below its accounts' own caps, "
    "which sum to 3 (default 3): at most 2 tasks run at once. Delete the key to make "
    "each account's max_concurrency the only limit, or keep it as a deliberate global cap"
)


class TestSupervisorWarning:
    @pytest.mark.usefixtures("private_global_lock")
    def test_warns_once_over_several_ticks(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        qd, config = _single_account_queue(tmp_path, "max_concurrency = 2")
        with caplog.at_level(logging.INFO, logger=DAEMON_LOGGER):
            _run_daemon(qd, config, pending_count_fn=lambda: 0, install_signal_handlers=False)
        assert _warnings(caplog) == [SINGLE_ACCOUNT_WARNING]

    @pytest.mark.usefixtures("private_global_lock")
    @pytest.mark.parametrize("concurrency", ["", "max_concurrency = 3", "max_concurrency = 4"])
    def test_no_warning_when_it_does_not_bind(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, concurrency: str
    ) -> None:
        qd, config = _single_account_queue(tmp_path, concurrency)
        with caplog.at_level(logging.INFO, logger=DAEMON_LOGGER):
            _run_daemon(qd, config, pending_count_fn=lambda: 0, install_signal_handlers=False)
        assert _warnings(caplog) == []

    @pytest.mark.allow_self_signal
    @pytest.mark.usefixtures("private_global_lock")
    def test_warns_once_more_after_a_reload(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        qd, config = _single_account_queue(tmp_path, "max_concurrency = 2")
        calls = {"n": 0}

        def pending_count() -> int:
            # Called once at startup and once per tick, after the tick's
            # ceiling check. The third call is in the second tick, so the
            # reload runs at the start of the third.
            calls["n"] += 1
            if calls["n"] == 3:
                os.kill(os.getpid(), signal.SIGHUP)
            return 0

        with caplog.at_level(logging.INFO, logger=DAEMON_LOGGER):
            _run_daemon(qd, config, pending_count_fn=pending_count, install_signal_handlers=True)
        assert any("SIGHUP received: reloaded config" in r.getMessage() for r in caplog.records)
        assert _warnings(caplog) == [SINGLE_ACCOUNT_WARNING, SINGLE_ACCOUNT_WARNING]


# ---------------------------------------------------------------------------
# Force-dispatch waits for a slot under the same ceiling
# ---------------------------------------------------------------------------


def _busy_slots(n: int, account: str, stop: threading.Event) -> dict[str, DispatchSlot]:
    """``n`` in-flight slots on ``account`` whose threads run until ``stop``."""
    slots: dict[str, DispatchSlot] = {}
    for i in range(n):
        thread = threading.Thread(target=stop.wait, kwargs={"timeout": 30}, daemon=True)
        thread.start()
        slots[f"busy-{account}-{i}"] = DispatchSlot(
            task_id=f"busy-{account}-{i}",
            account=account,
            started_at=datetime(2026, 10, 15, tzinfo=UTC),
            thread=thread,
        )
    return slots


class TestForceDispatch:
    @pytest.mark.parametrize(
        ("concurrency", "busy", "dispatched"),
        [
            ("", 7, 1),
            ("", 8, 0),
            ("max_concurrency = 5", 4, 1),
            ("max_concurrency = 5", 5, 0),
        ],
        ids=["omitted-7", "omitted-8", "explicit-4", "explicit-5"],
    )
    def test_a_request_waits_only_when_the_ceiling_is_full(
        self,
        tmp_path: Path,
        queue_dir: Path,
        two_accounts: dict[str, Path],
        concurrency: str,
        busy: int,
        dispatched: int,
    ) -> None:
        settings = _queue_toml(tmp_path / "claude_runner.toml", two_accounts, concurrency)
        _make_tasks(queue_dir, 1)
        fd_mod.write_request(queue_dir, "t00")
        stop = threading.Event()
        slots = _busy_slots(busy, "personal", stop)
        try:
            with _HeldDispatches() as held:
                n = fd_mod.tick_consume(
                    queue_dir=queue_dir, settings=settings, clock=RealClock(), in_flight_slots=slots
                )
                assert n == dispatched
                assert fd_mod.request_path(queue_dir, "t00").exists() is (dispatched == 0)
                held.release({k: v for k, v in slots.items() if k == "t00"})
        finally:
            stop.set()


# ---------------------------------------------------------------------------
# What the CLI shows
# ---------------------------------------------------------------------------


def _invoke(argv: list[str]) -> Any:
    return CliRunner().invoke(app, argv, env={"COLUMNS": "1000"})


class TestCli:
    def test_account_list_json_carries_the_ceiling(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        config = tmp_path / "claude_runner.toml"
        _queue_toml(config, two_accounts, "max_concurrency = 5")
        result = _invoke(
            ["account", "list", "--config", str(config), "--queue", str(queue_dir), "--json"]
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["queue_ceiling"] == {
            "ceiling": 5,
            "max_concurrency": 5,
            "accounts_total": 8,
            "account_caps": {"personal": 5, "work": 3},
            "binds": True,
            "initial_concurrency": None,
        }

    def test_account_list_prints_the_ceiling(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        config = tmp_path / "claude_runner.toml"
        _queue_toml(config, two_accounts, "initial_concurrency = 2")
        result = _invoke(["account", "list", "--config", str(config), "--queue", str(queue_dir)])
        assert result.exit_code == 0, result.output
        assert (
            "queue ceiling: 8, the sum of the accounts' max_concurrency (personal 5 + work 3); "
            "initial_concurrency 2 until a first task completes"
        ) in result.stdout

    def test_supervisor_status_shows_the_ceiling(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        _queue_toml(queue_dir / "claude_runner.toml", two_accounts, "max_concurrency = 5")
        text = _invoke(["supervisor", "status", "--queue", str(queue_dir)])
        assert text.exit_code == 0, text.output
        assert (
            "Queue ceiling:    5 from [concurrency].max_concurrency, which binds: the "
            "accounts' max_concurrency sum to 8 (personal 5 + work 3)"
        ) in text.stdout
        as_json = _invoke(["supervisor", "status", "--queue", str(queue_dir), "--json"])
        assert as_json.exit_code == 0, as_json.output
        ceiling = json.loads(as_json.stdout)["queue_ceiling"]
        assert (ceiling["ceiling"], ceiling["accounts_total"], ceiling["binds"]) == (5, 8, True)

    def test_supervisor_status_reports_an_unreadable_account_file(
        self, tmp_path: Path, queue_dir: Path, two_accounts: dict[str, Path]
    ) -> None:
        _queue_toml(queue_dir / "claude_runner.toml", two_accounts)
        (two_accounts["work"] / PER_ACCOUNT_TOML_NAME).write_text("not [toml", encoding="utf-8")
        result = _invoke(["supervisor", "status", "--queue", str(queue_dir), "--json"])
        assert result.exit_code == 0, result.output
        error = json.loads(result.stdout)["queue_ceiling"]["error"]
        assert str(two_accounts["work"] / PER_ACCOUNT_TOML_NAME) in error
