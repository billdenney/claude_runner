"""An account takes tasks only on a recent clean usage reading.

``state_machine.step`` stamps ``last_reading_at`` whenever it classifies a
clean reading. ``state_machine.expire_stale_readings`` moves every account
that takes tasks but whose reading is older than
``[usage].max_reading_age_s`` (or missing) to NO_READING, and the daemon's
``run_one_tick`` runs it on every account each tick. End-to-end dispatch
counts are in ``tests/integration/test_throttle_dispatch.py``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from claude_task_runner.clock import FakeClock
from claude_task_runner.config.loader import load_settings
from claude_task_runner.config.schema import AccountSettings, Settings
from claude_task_runner.runner.account_dispatch import _DISPATCHABLE_STATES
from claude_task_runner.supervisor.actions import Action, EmitEvent, Notify
from claude_task_runner.supervisor.daemon import PollResult, TickContext, run_one_tick
from claude_task_runner.supervisor.persistence import initial_snapshot
from claude_task_runner.supervisor.state_machine import expire_stale_readings
from claude_task_runner.supervisor.states import (
    AccountState,
    SupervisorSnapshot,
    SupervisorState,
)
from claude_task_runner.usage.drift import UsageCaptureTimeout, UsageFormatDrift
from claude_task_runner.usage.models import UsageReading, WindowReading

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
LIMIT_S = 600.0

TAKES_TASKS = {SupervisorState.IDLE, SupervisorState.DISPATCHING, SupervisorState.SLOWING_DOWN}
"""Written out rather than read from ``_DISPATCHABLE_STATES``, so that a
change to which states take tasks has to be made here too."""


def _account(
    state: SupervisorState,
    *,
    read_at: datetime | None,
    target: int | None = 3,
) -> AccountState:
    return AccountState(
        state=state,
        since=datetime(2026, 9, 26, 11, 0, tzinfo=UTC),
        last_5h_util_pct=45,
        last_weekly_util_pct=20,
        scheduled_wakeup_at=datetime(2026, 9, 26, 14, 0, tzinfo=UTC),
        target_concurrency=target,
        last_capture_at=datetime(2026, 9, 26, 11, 59, tzinfo=UTC),
        last_reading_at=read_at,
    )


def _snapshot(**accounts: AccountState) -> SupervisorSnapshot:
    return SupervisorSnapshot(
        state=SupervisorState.DISPATCHING,
        since=datetime(2026, 9, 26, 11, 0, tzinfo=UTC),
        accounts=accounts,
    )


class TestExpireStaleReadings:
    def test_takes_tasks_names_the_dispatchable_states(self) -> None:
        assert frozenset(TAKES_TASKS) == _DISPATCHABLE_STATES

    def test_reading_exactly_at_the_limit_is_kept(self) -> None:
        snap = _snapshot(
            a=_account(SupervisorState.DISPATCHING, read_at=NOW - timedelta(seconds=600))
        )
        new, actions = expire_stale_readings(snap, now=NOW, max_reading_age_s=LIMIT_S)
        assert new == snap
        assert actions == []

    def test_reading_just_past_the_limit_moves(self) -> None:
        read_at = NOW - timedelta(seconds=600, microseconds=1)
        snap = _snapshot(a=_account(SupervisorState.DISPATCHING, read_at=read_at))
        new, _ = expire_stale_readings(snap, now=NOW, max_reading_age_s=LIMIT_S)
        assert new.accounts["a"].state is SupervisorState.NO_READING

    @pytest.mark.parametrize("state", list(SupervisorState))
    def test_only_states_that_take_tasks_move(self, state: SupervisorState) -> None:
        stale = NOW - timedelta(hours=2)
        snap = _snapshot(a=_account(state, read_at=stale))
        new, actions = expire_stale_readings(snap, now=NOW, max_reading_age_s=LIMIT_S)
        if state in TAKES_TASKS:
            assert new.accounts["a"].state is SupervisorState.NO_READING
            assert len(actions) == 2
        else:
            assert new == snap
            assert actions == []

    def test_moved_account_loses_its_decision_and_keeps_the_rest(self) -> None:
        read_at = datetime(2026, 9, 26, 11, 0, tzinfo=UTC)
        before = _account(SupervisorState.SLOWING_DOWN, read_at=read_at)
        new, actions = expire_stale_readings(
            _snapshot(a=before), now=NOW, max_reading_age_s=LIMIT_S
        )
        assert new.accounts["a"] == before.model_copy(
            update={
                "state": SupervisorState.NO_READING,
                "since": NOW,
                "target_concurrency": None,
                "scheduled_wakeup_at": None,
            }
        )
        assert actions == [
            Notify(
                level="warn",
                message=(
                    "no clean usage reading for account 'a' since 2026-09-26 11:00:00 UTC, "
                    "over the 600 s limit; no tasks go through it until a capture succeeds"
                ),
            ),
            EmitEvent(
                event_type="state_transition",
                payload={
                    "from": "slowing_down",
                    "to": "no_reading",
                    "five_hour_util": 45,
                    "weekly_util": 20,
                },
            ),
        ]

    def test_account_never_read_moves(self) -> None:
        snap = _snapshot(a=_account(SupervisorState.IDLE, read_at=None))
        new, actions = expire_stale_readings(snap, now=NOW, max_reading_age_s=LIMIT_S)
        assert new.accounts["a"].state is SupervisorState.NO_READING
        assert actions[0] == Notify(
            level="warn",
            message=(
                "no clean usage reading for account 'a' yet; "
                "no tasks go through it until a capture succeeds"
            ),
        )

    def test_only_the_stale_account_moves_and_the_top_level_is_left(self) -> None:
        snap = _snapshot(
            fresh=_account(SupervisorState.DISPATCHING, read_at=NOW - timedelta(seconds=30)),
            stale=_account(SupervisorState.DISPATCHING, read_at=NOW - timedelta(hours=1)),
        )
        new, actions = expire_stale_readings(snap, now=NOW, max_reading_age_s=LIMIT_S)
        assert new.accounts["fresh"] == snap.accounts["fresh"]
        assert new.accounts["stale"].state is SupervisorState.NO_READING
        assert new.state is SupervisorState.DISPATCHING
        assert [a.message for a in actions if isinstance(a, Notify)] == [
            "no clean usage reading for account 'stale' since 2026-09-26 11:00:00 UTC, "
            "over the 600 s limit; no tasks go through it until a capture succeeds"
        ]


def _reading(util_5h: int, account: str | None) -> UsageReading:
    return UsageReading(
        captured_at=NOW,
        five_hour=WindowReading(
            utilization_pct=util_5h, resets_at_raw="x", resets_at=NOW + timedelta(hours=2)
        ),
        seven_day=WindowReading(
            utilization_pct=5, resets_at_raw="x", resets_at=NOW + timedelta(days=4)
        ),
        account=account,
    )


def _settings(*names: str) -> Settings:
    """Package defaults (600 s limit, 60 s polls) with these accounts, in UTC."""
    base = load_settings(None)
    return base.model_copy(
        update={
            "accounts": [AccountSettings(name=n, config_dir="") for n in names],
            "dispatch_pct": base.dispatch_pct.model_copy(update={"timezone": "UTC"}),
        }
    )


def _tick(
    snap: SupervisorSnapshot,
    settings: Settings,
    poll: PollResult,
    clock: FakeClock,
    *,
    pending: int = 3,
) -> tuple[SupervisorSnapshot, list[Action]]:
    return run_one_tick(
        snap,
        TickContext(settings=settings, poll_result=poll, pending_count=pending, in_flight_count=0),
        clock,
    )


class TestStepStampsTheReading:
    """``last_reading_at`` moves on every clean reading and on nothing else."""

    @pytest.mark.parametrize("pending", [0, 3], ids=["idle", "dispatching"])
    def test_clean_reading_stamps_it(self, pending: int) -> None:
        clock = FakeClock(NOW)
        snap, _ = _tick(
            initial_snapshot(since=NOW, account_names=["default"]),
            _settings("default"),
            _reading(10, None),
            clock,
            pending=pending,
        )
        assert snap.accounts["default"].last_reading_at == NOW
        assert snap.last_reading_at == NOW

    @pytest.mark.parametrize(
        "poll",
        [UsageCaptureTimeout("slow"), UsageFormatDrift("only 1 block found")],
        ids=["capture_timeout", "parser_drift"],
    )
    def test_failed_capture_leaves_it(self, poll: PollResult) -> None:
        clock = FakeClock(NOW)
        settings = _settings("default")
        snap, _ = _tick(
            initial_snapshot(since=NOW, account_names=["default"]),
            settings,
            _reading(10, None),
            clock,
        )
        clock.advance(60)
        snap, _ = _tick(snap, settings, poll, clock)
        assert snap.accounts["default"].last_reading_at == NOW
        assert snap.accounts["default"].last_capture_at == NOW + timedelta(seconds=60)

    def test_clean_poll_while_recovering_from_drift_stamps_it(self) -> None:
        clock = FakeClock(NOW)
        settings = _settings("default")
        snap, _ = _tick(
            initial_snapshot(since=NOW, account_names=["default"]),
            settings,
            UsageFormatDrift("only 1 block found"),
            clock,
        )
        clock.advance(60)
        snap, _ = _tick(snap, settings, _reading(10, None), clock)
        assert snap.accounts["default"].state is SupervisorState.ERROR_DRIFT
        assert snap.accounts["default"].last_reading_at == NOW + timedelta(seconds=60)


class TestRunOneTick:
    def test_top_level_follows_the_account_read_this_tick_into_no_reading(self) -> None:
        clock = FakeClock(NOW)
        settings = _settings("default")
        snap, _ = _tick(
            initial_snapshot(since=NOW, account_names=["default"]),
            settings,
            _reading(10, None),
            clock,
        )
        clock.advance(601)
        snap, actions = _tick(snap, settings, UsageCaptureTimeout("slow"), clock)
        assert snap.accounts["default"].state is SupervisorState.NO_READING
        assert snap.state is SupervisorState.NO_READING
        assert snap.target_concurrency is None
        assert [a.level for a in actions if isinstance(a, Notify)] == ["warn"]

    def test_top_level_stays_on_the_account_read_this_tick(self) -> None:
        """``work`` expires on the tick that reads ``personal``: the
        top-level view keeps showing ``personal``."""
        clock = FakeClock(NOW)
        settings = _settings("personal", "work")
        snap = initial_snapshot(since=NOW, account_names=["personal", "work"])
        snap, _ = _tick(snap, settings, _reading(30, "work"), clock)
        clock.advance(601)
        snap, _ = _tick(snap, settings, _reading(10, "personal"), clock)
        assert snap.accounts["work"].state is SupervisorState.NO_READING
        assert snap.state is SupervisorState.DISPATCHING
        assert snap.last_5h_util_pct == 10

    def test_unnamed_reading_on_a_multi_account_queue_logs_an_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        clock = FakeClock(NOW)
        settings = _settings("personal", "work")
        snap = initial_snapshot(since=NOW, account_names=["personal", "work"])
        with caplog.at_level(logging.ERROR, logger="claude_task_runner.supervisor.daemon"):
            new, _ = _tick(snap, settings, _reading(10, None), clock)
        assert [r.getMessage() for r in caplog.records] == [
            "usage poll result names no account, but 2 accounts are configured, so no "
            "account's state is updated and each stops taking tasks once its last "
            "reading is over [usage].max_reading_age_s old. Restart the supervisor "
            "after changing [[accounts]]."
        ]
        assert new.accounts == snap.accounts

    def test_named_reading_logs_nothing(self, caplog: pytest.LogCaptureFixture) -> None:
        clock = FakeClock(NOW)
        settings = _settings("personal", "work")
        snap = initial_snapshot(since=NOW, account_names=["personal", "work"])
        with caplog.at_level(logging.ERROR, logger="claude_task_runner.supervisor.daemon"):
            _tick(snap, settings, _reading(10, "work"), clock)
        assert caplog.records == []
