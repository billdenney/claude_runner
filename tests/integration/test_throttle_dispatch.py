"""Dispatch applies the throttle decision the operator is told (ADR-0022).

These tests drive the real daemon tick (:func:`run_one_tick`) and the
real orchestrator (:func:`tick_dispatch`), then count the tasks
dispatched through each account.

* **SLOWING_DOWN.** The supervisor notifies ``slowing dispatch: ...
  target concurrency=X/Y``. ``X`` is ADR-0022's linear ramp: the
  account's ``max_concurrency`` at ``fivehr_slowdown_pct``, falling to 0
  at ``fivehr_stop_pct``. Dispatch must run exactly ``X`` tasks through
  that account.
* **Throttled or drifting.** THROTTLED_5H, THROTTLED_WEEKLY and
  ERROR_DRIFT mean no new dispatch, on a single-account queue too.
* **IDLE.** An account captured while nothing was pending goes IDLE,
  which is dispatchable, but tasks that arrive before its next capture
  run under the cap of that reading.
* **No fresh reading.** An account whose last clean reading is older
  than ``[usage].max_reading_age_s``, or that has none, is NO_READING
  and takes no tasks until a capture succeeds.

The multi-account configuration mirrors a live two-account queue:
``personal`` allows 5 concurrent tasks, ``work`` allows 1, and the
queue-wide ``[concurrency]`` block allows 5.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from claude_task_runner.clock import FakeClock
from claude_task_runner.config.loader import load_settings, resolve_accounts
from claude_task_runner.config.schema import Settings
from claude_task_runner.queue.schema import Task
from claude_task_runner.queue.store import (
    queue_runtime_dir,
    task_path_for,
    todo_dir,
    write_task_atomic,
)
from claude_task_runner.runner import orchestrator as orch_mod
from claude_task_runner.runner.in_flight import DispatchSlot
from claude_task_runner.supervisor import persistence as persist_mod
from claude_task_runner.supervisor.actions import Action, Notify
from claude_task_runner.supervisor.daemon import PollResult, TickContext, run_one_tick
from claude_task_runner.supervisor.states import SupervisorSnapshot, SupervisorState
from claude_task_runner.usage.drift import UsageCaptureTimeout, UsageFormatDrift
from claude_task_runner.usage.models import UsageReading, WindowReading
from claude_task_runner.usage.multi_account_source import MultiAccountSourceError

NOW = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
"""Noon UTC: inside the default day band (slowdown 40, stop 60)."""

PENDING = 8
"""More pending tasks than any cap in these tests, so caps decide the count."""


def _write_account_policy(config_dir: Path, max_concurrency: int) -> None:
    config_dir.mkdir(parents=True)
    (config_dir / "runner-account.toml").write_text(
        f"[concurrency]\nmax_concurrency = {max_concurrency}\n", encoding="utf-8"
    )


def _load(tmp_path: Path, body: str) -> Settings:
    toml = tmp_path / "claude_runner.toml"
    toml.write_text('[dispatch_pct]\ntimezone = "UTC"\n\n' + body, encoding="utf-8")
    return load_settings(toml)


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    for i in range(PENDING):
        task = Task.model_validate({"id": f"t{i}", "title": f"Task t{i}", "prompt": "p"})
        write_task_atomic(task, task_path_for(qd, task.id))
    return qd


def _reading(five_hour_pct: int, account: str | None, *, weekly_pct: int = 5) -> UsageReading:
    """A clean reading. The weekly window resets in 4 days, so the trace
    target is about 34%: the default 5% is under it, 90% is over it."""
    return UsageReading(
        captured_at=NOW,
        five_hour=WindowReading(
            utilization_pct=five_hour_pct,
            resets_at_raw="x",
            resets_at=NOW + timedelta(hours=2),
        ),
        seven_day=WindowReading(
            utilization_pct=weekly_pct,
            resets_at_raw="y",
            resets_at=NOW + timedelta(days=4),
        ),
        account=account,
    )


def _two_account_settings(tmp_path: Path) -> Settings:
    """``personal`` allows 5, ``work`` allows 1, the queue allows 5."""
    _write_account_policy(tmp_path / "personal", max_concurrency=5)
    _write_account_policy(tmp_path / "work", max_concurrency=1)
    return _load(
        tmp_path,
        "[concurrency]\nmax_concurrency = 5\ninitial_concurrency = 5\n\n"
        f'[[accounts]]\nname = "personal"\nconfig_dir = "{tmp_path / "personal"}"\n\n'
        f'[[accounts]]\nname = "work"\nconfig_dir = "{tmp_path / "work"}"\n',
    )


def _single_account_settings(tmp_path: Path) -> Settings:
    """No ``[[accounts]]`` block: one ``default`` account, which allows 4,
    as does the queue."""
    _write_account_policy(tmp_path / "claude", max_concurrency=4)
    return _load(
        tmp_path,
        "[concurrency]\nmax_concurrency = 4\ninitial_concurrency = 4\n\n"
        f'[claude]\nconfig_dir = "{tmp_path / "claude"}"\n',
    )


def _tick(
    snapshot: SupervisorSnapshot,
    settings: Settings,
    reading: PollResult,
    clock: FakeClock,
    *,
    pending_count: int = PENDING,
) -> tuple[SupervisorSnapshot, list[Action]]:
    """One supervisor tick, built the way ``start_daemon`` builds it."""
    ctx = TickContext(
        settings=settings,
        poll_result=reading,
        pending_count=pending_count,
        in_flight_count=0,
        account_policies={a.name: a.policy for a in resolve_accounts(settings)},
    )
    return run_one_tick(snapshot, ctx, clock)


def _dispatch_counts(
    queue_dir: Path, settings: Settings, snapshot: SupervisorSnapshot, clock: FakeClock
) -> dict[str, int]:
    """Run one real ``tick_dispatch`` and count dispatched tasks per account.

    The dispatcher is stubbed, and every dispatch thread is joined while
    the stub is still in place, so no ``claude`` process can start.
    """
    slots: dict[str, DispatchSlot] = {}

    def _no_op_dispatch(**_kwargs: Any) -> None:
        return None

    with patch.object(orch_mod.dispatcher_mod, "dispatch", side_effect=_no_op_dispatch):
        orch_mod.tick_dispatch(
            queue_dir=queue_dir,
            settings=settings,
            clock=clock,
            snapshot=snapshot,
            in_flight_slots=slots,
        )
        for slot in slots.values():
            slot.thread.join(timeout=5)
    counts: dict[str, int] = {}
    for slot in slots.values():
        counts[slot.account] = counts.get(slot.account, 0) + 1
    return counts


def _slowdown_messages(actions: list[Action]) -> list[str]:
    return [
        a.message
        for a in actions
        if isinstance(a, Notify) and a.message.startswith("slowing dispatch")
    ]


class TestMultiAccountSlowdown:
    """``personal`` is slowing down at 55% 5h; ``work`` is dispatching at 10%.

    The ramp for ``personal`` is ``ceil(5 * (1 - (55 - 40) / (60 - 40)))
    = 2``. ``work`` is not slowing down, so it keeps its cap of 1. The
    order in which the two accounts were captured must not matter.
    """

    @pytest.mark.parametrize(
        "capture_order",
        [("personal", "work"), ("work", "personal")],
        ids=["personal-then-work", "work-then-personal"],
    )
    def test_slowed_account_dispatches_its_ramp_target(
        self, tmp_path: Path, queue_dir: Path, capture_order: tuple[str, str]
    ) -> None:
        settings = _two_account_settings(tmp_path)
        clock = FakeClock(NOW)
        snapshot = persist_mod.initial_snapshot(since=NOW, account_names=["personal", "work"])
        five_hour_pct = {"personal": 55, "work": 10}

        actions: list[Action] = []
        for name in capture_order:
            snapshot, tick_actions = _tick(
                snapshot, settings, _reading(five_hour_pct[name], name), clock
            )
            actions.extend(tick_actions)

        assert snapshot.accounts["personal"].state is SupervisorState.SLOWING_DOWN
        assert snapshot.accounts["work"].state is SupervisorState.DISPATCHING
        assert _slowdown_messages(actions) == [
            "slowing dispatch: 5h=55% in [40, 60) (day); target concurrency=2/5"
        ]

        counts = _dispatch_counts(queue_dir, settings, snapshot, clock)

        assert counts == {"personal": 2, "work": 1}


class TestMultiAccountIdle:
    """An account that went IDLE keeps the cap its last reading called for.

    Each tick captures one account, so an account classified IDLE while
    the queue was empty stays IDLE until its next capture, one round-robin
    cycle later. Tasks that arrive in between must not run through it
    beyond what that reading allows: nothing if it was throttled, the ramp
    target if it was slowing down, its ``max_concurrency`` otherwise.
    """

    @pytest.mark.parametrize(
        ("five_hour_pct", "weekly_pct", "expected"),
        [
            # The queue-wide ceiling of 5 leaves ``personal`` 4 of its 5.
            (30, 5, {"work": 1, "personal": 4}),
            # Ramp: ceil(5 * (1 - (55 - 40) / (60 - 40))) = 2.
            (55, 5, {"work": 1, "personal": 2}),
            (65, 5, {"work": 1}),
            (10, 90, {"work": 1}),
        ],
        ids=["dispatching", "slowing_down", "throttled_5h", "throttled_weekly"],
    )
    def test_idle_account_dispatches_what_its_reading_allows(
        self,
        tmp_path: Path,
        queue_dir: Path,
        five_hour_pct: int,
        weekly_pct: int,
        expected: dict[str, int],
    ) -> None:
        """``personal`` is captured while nothing is pending, so it goes
        IDLE. Then eight tasks arrive and ``work`` is captured at 10%."""
        settings = _two_account_settings(tmp_path)
        clock = FakeClock(NOW)
        snapshot = persist_mod.initial_snapshot(since=NOW, account_names=["personal", "work"])

        snapshot, _ = _tick(
            snapshot,
            settings,
            _reading(five_hour_pct, "personal", weekly_pct=weekly_pct),
            clock,
            pending_count=0,
        )
        snapshot, _ = _tick(snapshot, settings, _reading(10, "work"), clock)

        assert snapshot.accounts["personal"].state is SupervisorState.IDLE
        assert snapshot.accounts["work"].state is SupervisorState.DISPATCHING
        assert _dispatch_counts(queue_dir, settings, snapshot, clock) == expected


def _capture_timeout(account: str) -> PollResult:
    """A capture of ``account`` that timed out, as the multi-account source reports it."""
    return MultiAccountSourceError.wrap(account, UsageCaptureTimeout("slow"))


def _warnings(actions: list[Action]) -> list[str]:
    return [a.message for a in actions if isinstance(a, Notify) and a.level == "warn"]


class TestFreshReadingRequired:
    """An account takes tasks only while its last clean reading is recent.

    ``[usage].max_reading_age_s`` (600 s by default) bounds the age of that
    reading. Failed captures don't count. An account never read, or whose
    last reading is older than the limit, is NO_READING, and dispatch skips
    it until a capture succeeds.
    """

    def test_account_never_read_takes_no_tasks(self, tmp_path: Path, queue_dir: Path) -> None:
        """Cold start: the first tick reads ``personal``; ``work`` has not
        been read yet. It used to take a task at 0% on no data."""
        settings = _two_account_settings(tmp_path)
        clock = FakeClock(NOW)
        snapshot = persist_mod.initial_snapshot(since=NOW, account_names=["personal", "work"])

        snapshot, actions = _tick(snapshot, settings, _reading(10, "personal"), clock)

        assert snapshot.accounts["work"].state is SupervisorState.NO_READING
        assert snapshot.accounts["work"].target_concurrency is None
        assert _warnings(actions) == []
        assert _dispatch_counts(queue_dir, settings, snapshot, clock) == {"personal": 5}

    def test_account_whose_captures_always_fail_takes_no_tasks(
        self, tmp_path: Path, queue_dir: Path
    ) -> None:
        """Every capture of ``personal`` times out while ``work`` is over its
        5h stop. ``personal`` used to stay in its seeded IDLE at 0%, sort
        first and take all 5 tasks with no usage data at all."""
        settings = _two_account_settings(tmp_path)
        clock = FakeClock(NOW)
        snapshot = persist_mod.initial_snapshot(since=NOW, account_names=["personal", "work"])

        for _ in range(3):
            snapshot, _ = _tick(snapshot, settings, _capture_timeout("personal"), clock)
            snapshot, _ = _tick(snapshot, settings, _reading(65, "work"), clock)
            clock.advance(60)

        assert snapshot.accounts["personal"].state is SupervisorState.NO_READING
        assert _dispatch_counts(queue_dir, settings, snapshot, clock) == {}

    def test_account_stops_once_its_last_reading_is_too_old(
        self, tmp_path: Path, queue_dir: Path
    ) -> None:
        """``personal`` reads 10% at noon, then every capture of it fails. At
        exactly 600 s it still takes tasks; one second later it stops, and
        the supervisor says so once."""
        settings = _two_account_settings(tmp_path)
        clock = FakeClock(NOW)
        snapshot = persist_mod.initial_snapshot(since=NOW, account_names=["personal", "work"])
        snapshot, _ = _tick(snapshot, settings, _reading(10, "personal"), clock)
        snapshot, _ = _tick(snapshot, settings, _reading(65, "work"), clock)

        clock.advance(600)
        snapshot, actions = _tick(snapshot, settings, _capture_timeout("personal"), clock)
        assert snapshot.accounts["personal"].state is SupervisorState.DISPATCHING
        assert _warnings(actions) == []
        assert _dispatch_counts(queue_dir, settings, snapshot, clock) == {"personal": 5}

        clock.advance(1)
        snapshot, actions = _tick(snapshot, settings, _capture_timeout("personal"), clock)
        assert snapshot.accounts["personal"].state is SupervisorState.NO_READING
        assert snapshot.accounts["personal"].target_concurrency is None
        assert _warnings(actions) == [
            "no clean usage reading for account 'personal' since 2026-05-27 12:00:00 UTC, "
            "over the 600 s limit; no tasks go through it until a capture succeeds"
        ]

        snapshot, actions = _tick(snapshot, settings, _capture_timeout("personal"), clock)
        assert _warnings(actions) == []

    def test_account_takes_tasks_again_once_a_capture_succeeds(
        self, tmp_path: Path, queue_dir: Path
    ) -> None:
        settings = _two_account_settings(tmp_path)
        clock = FakeClock(NOW)
        snapshot = persist_mod.initial_snapshot(since=NOW, account_names=["personal", "work"])
        snapshot, _ = _tick(snapshot, settings, _reading(10, "personal"), clock)
        clock.advance(601)
        snapshot, _ = _tick(snapshot, settings, _capture_timeout("personal"), clock)
        assert snapshot.accounts["personal"].state is SupervisorState.NO_READING

        snapshot, _ = _tick(snapshot, settings, _reading(10, "personal"), clock)

        assert snapshot.accounts["personal"].state is SupervisorState.DISPATCHING
        assert _dispatch_counts(queue_dir, settings, snapshot, clock) == {"personal": 5}

    def test_restart_after_downtime_waits_for_each_accounts_reading(
        self, tmp_path: Path, queue_dir: Path
    ) -> None:
        """Both accounts read clean at noon; the supervisor is then down for
        two hours. Its first tick reads only ``work``, so ``personal`` must
        not take tasks on a two-hour-old reading."""
        settings = _two_account_settings(tmp_path)
        clock = FakeClock(NOW)
        snapshot = persist_mod.initial_snapshot(since=NOW, account_names=["personal", "work"])
        snapshot, _ = _tick(snapshot, settings, _reading(10, "personal"), clock)
        snapshot, _ = _tick(snapshot, settings, _reading(10, "work"), clock)

        clock.advance(2 * 3600)
        snapshot, _ = _tick(snapshot, settings, _reading(10, "work"), clock)

        assert snapshot.accounts["personal"].state is SupervisorState.NO_READING
        assert _dispatch_counts(queue_dir, settings, snapshot, clock) == {"work": 1}

    def test_single_account_queue_dispatches_on_its_first_tick(
        self, tmp_path: Path, queue_dir: Path
    ) -> None:
        """A single-account queue reads its account before each dispatch
        pass, so requiring a reading costs it nothing at start."""
        settings = _single_account_settings(tmp_path)
        clock = FakeClock(NOW)
        snapshot = persist_mod.initial_snapshot(since=NOW, account_names=["default"])
        assert snapshot.accounts["default"].state is SupervisorState.NO_READING

        snapshot, _ = _tick(snapshot, settings, _reading(10, None), clock)

        assert snapshot.state is SupervisorState.DISPATCHING
        assert _dispatch_counts(queue_dir, settings, snapshot, clock) == {"default": 4}

    def test_single_account_queue_whose_first_capture_fails_waits(
        self, tmp_path: Path, queue_dir: Path
    ) -> None:
        settings = _single_account_settings(tmp_path)
        clock = FakeClock(NOW)
        snapshot = persist_mod.initial_snapshot(since=NOW, account_names=["default"])

        snapshot, _ = _tick(snapshot, settings, UsageCaptureTimeout("slow"), clock)

        assert snapshot.state is SupervisorState.NO_READING
        assert snapshot.accounts["default"].state is SupervisorState.NO_READING
        assert _dispatch_counts(queue_dir, settings, snapshot, clock) == {}


class TestSingleAccountSlowdown:
    """A queue with no ``[[accounts]]`` block: one ``default`` account.

    Its readings carry no account name, which is what the single-account
    usage source produces. The account and the queue both allow 4.
    """

    @pytest.mark.parametrize(
        ("five_hour_pct", "ramp_target"),
        [(45, 3), (55, 1)],
        ids=["45pct-ramp-3", "55pct-ramp-1"],
    )
    def test_dispatches_the_ramp_target(
        self, tmp_path: Path, queue_dir: Path, five_hour_pct: int, ramp_target: int
    ) -> None:
        settings = _single_account_settings(tmp_path)
        clock = FakeClock(NOW)
        snapshot = persist_mod.initial_snapshot(since=NOW, account_names=["default"])

        snapshot, actions = _tick(snapshot, settings, _reading(five_hour_pct, None), clock)

        assert snapshot.state is SupervisorState.SLOWING_DOWN
        assert _slowdown_messages(actions) == [
            f"slowing dispatch: 5h={five_hour_pct}% in [40, 60) (day); "
            f"target concurrency={ramp_target}/4"
        ]

        counts = _dispatch_counts(queue_dir, settings, snapshot, clock)

        assert counts == {"default": ramp_target}


class TestSingleAccountThrottled:
    """A single-account queue dispatches nothing while throttled or drifting.

    Dispatch gates on each account's own state, so an unnamed reading has
    to reach ``accounts["default"]``, not only the top-level view.
    """

    @pytest.mark.parametrize(
        ("poll_result", "expected"),
        [
            (_reading(65, None), SupervisorState.THROTTLED_5H),
            (_reading(10, None, weekly_pct=90), SupervisorState.THROTTLED_WEEKLY),
            (UsageFormatDrift("only 1 block found"), SupervisorState.ERROR_DRIFT),
        ],
        ids=["throttled_5h", "throttled_weekly", "error_drift"],
    )
    def test_dispatches_nothing(
        self,
        tmp_path: Path,
        queue_dir: Path,
        poll_result: UsageReading | UsageFormatDrift,
        expected: SupervisorState,
    ) -> None:
        settings = _single_account_settings(tmp_path)
        clock = FakeClock(NOW)
        snapshot = persist_mod.initial_snapshot(since=NOW, account_names=["default"])

        snapshot, _ = _tick(snapshot, settings, poll_result, clock)

        assert snapshot.state is expected
        assert snapshot.accounts["default"].state is expected
        assert _dispatch_counts(queue_dir, settings, snapshot, clock) == {}

    def test_dispatches_again_once_the_reading_recovers(
        self, tmp_path: Path, queue_dir: Path
    ) -> None:
        settings = _single_account_settings(tmp_path)
        clock = FakeClock(NOW)
        snapshot = persist_mod.initial_snapshot(since=NOW, account_names=["default"])

        snapshot, _ = _tick(snapshot, settings, _reading(65, None), clock)
        snapshot, _ = _tick(snapshot, settings, _reading(10, None), clock)

        assert snapshot.accounts["default"].state is SupervisorState.DISPATCHING
        assert _dispatch_counts(queue_dir, settings, snapshot, clock) == {"default": 4}


class TestSingleAccountIdle:
    """A single-account queue captures its account every tick, so a stale
    IDLE lasts only while a capture fails: a failed capture leaves the
    state as it was, and dispatch still runs that tick."""

    @pytest.mark.parametrize(
        ("idle_reading", "expected"),
        [
            (_reading(65, None), {}),
            (_reading(10, None, weekly_pct=90), {}),
            # Ramp: ceil(4 * (1 - (55 - 40) / (60 - 40))) = 1.
            (_reading(55, None), {"default": 1}),
        ],
        ids=["throttled_5h", "throttled_weekly", "slowing_down"],
    )
    def test_idle_cap_holds_through_a_failed_capture(
        self,
        tmp_path: Path,
        queue_dir: Path,
        idle_reading: UsageReading,
        expected: dict[str, int],
    ) -> None:
        """The queue is empty at the first capture, so the account goes
        IDLE. Then eight tasks arrive and the next capture times out."""
        settings = _single_account_settings(tmp_path)
        clock = FakeClock(NOW)
        snapshot = persist_mod.initial_snapshot(since=NOW, account_names=["default"])

        snapshot, _ = _tick(snapshot, settings, idle_reading, clock, pending_count=0)
        snapshot, _ = _tick(snapshot, settings, UsageCaptureTimeout("slow"), clock)

        assert snapshot.accounts["default"].state is SupervisorState.IDLE
        assert _dispatch_counts(queue_dir, settings, snapshot, clock) == expected
