"""Tests for ``throttle.decision.decide`` (ADR-0022).

Exercises the decision rule end to end:

* Weekly first: observed > target ⇒ THROTTLED_WEEKLY.
* Else 5h: pick day/night, classify against slowdown/stop.
* Wakeup clamping: never busy-spin, never sleep past next 5h reset.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from claude_task_runner.clock import FakeClock
from claude_task_runner.supervisor.states import SupervisorState
from claude_task_runner.throttle.curve import SEVEN_DAYS_S
from claude_task_runner.throttle.decision import decide
from claude_task_runner.throttle.policy import (
    ResolvedBand,
    ResolvedNight,
    ResolvedPolicy,
    ResolvedWeek,
)
from claude_task_runner.usage.models import UsageReading, WindowReading

POLL = 60.0


def _policy(
    *,
    max_concurrency: int = 5,
    timezone: str = "",
    day_slow: int = 40,
    day_stop: int = 60,
    night_slow: int = 70,
    night_stop: int = 90,
    night_start: time = time(21, 0),
    night_end: time = time(6, 0),
    early_pct: int = 60,
    eow_pct: int = 95,
    eow_switch_s: float = 40 * 3600,
) -> ResolvedPolicy:
    return ResolvedPolicy(
        account_name="t",
        max_concurrency=max_concurrency,
        timezone=timezone,
        day=ResolvedBand(fivehr_slowdown_pct=day_slow, fivehr_stop_pct=day_stop),
        night=ResolvedNight(
            fivehr_slowdown_pct=night_slow,
            fivehr_stop_pct=night_stop,
            time_start=night_start,
            time_end=night_end,
        ),
        week=ResolvedWeek(early_pct=early_pct, eow_pct=eow_pct, eow_time_switch_s=eow_switch_s),
    )


def _reading(
    *,
    five_h_pct: int,
    weekly_pct: int,
    five_h_resets_at: datetime | None,
    weekly_resets_at: datetime | None,
    captured_at: datetime | None = None,
) -> UsageReading:
    captured = captured_at or datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
    return UsageReading(
        captured_at=captured,
        five_hour=WindowReading(
            utilization_pct=five_h_pct,
            resets_at_raw="x",
            resets_at=five_h_resets_at,
        ),
        seven_day=WindowReading(
            utilization_pct=weekly_pct,
            resets_at_raw="y",
            resets_at=weekly_resets_at,
        ),
    )


class TestDispatching:
    def test_low_util_returns_dispatching(self) -> None:
        # Place 'now' in the middle of the day band so we use day thresholds.
        now = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
        clock = FakeClock(now)
        reading = _reading(
            five_h_pct=10,
            weekly_pct=20,
            five_h_resets_at=now + timedelta(hours=2),
            weekly_resets_at=now + timedelta(days=4),
        )
        d = decide(_policy(), reading, clock, poll_interval_s=POLL)
        assert d.state is SupervisorState.DISPATCHING
        assert d.target_concurrency == 5
        assert d.wakeup_at is None
        assert d.band == "day"


class TestSlowingDown:
    def test_5h_in_slow_band_ramps_concurrency(self) -> None:
        now = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
        clock = FakeClock(now)
        reading = _reading(
            five_h_pct=50,  # midway between 40 and 60 → ramp at halfway
            weekly_pct=20,
            five_h_resets_at=now + timedelta(hours=2),
            weekly_resets_at=now + timedelta(days=4),
        )
        d = decide(_policy(max_concurrency=5), reading, clock, poll_interval_s=POLL)
        assert d.state is SupervisorState.SLOWING_DOWN
        # Linear ramp: progress = (50-40)/(60-40) = 0.5; ceil(5 * 0.5) = 3.
        assert d.target_concurrency == 3
        assert d.wakeup_at is not None

    def test_slow_at_band_edge_top(self) -> None:
        # observed exactly at slowdown_pct — should ramp at top of band (max concurrency).
        now = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
        clock = FakeClock(now)
        reading = _reading(
            five_h_pct=40,
            weekly_pct=10,
            five_h_resets_at=now + timedelta(hours=2),
            weekly_resets_at=now + timedelta(days=4),
        )
        d = decide(_policy(max_concurrency=5), reading, clock, poll_interval_s=POLL)
        assert d.state is SupervisorState.SLOWING_DOWN
        # progress = 0 → ceil(5 * 1.0) = 5.
        assert d.target_concurrency == 5


class TestThrottled5h:
    def test_at_stop_band_returns_throttled(self) -> None:
        now = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
        clock = FakeClock(now)
        reading = _reading(
            five_h_pct=60,
            weekly_pct=20,
            five_h_resets_at=now + timedelta(hours=2),
            weekly_resets_at=now + timedelta(days=4),
        )
        d = decide(_policy(), reading, clock, poll_interval_s=POLL)
        assert d.state is SupervisorState.THROTTLED_5H
        assert d.target_concurrency == 0
        assert d.wakeup_at == now + timedelta(hours=2)

    def test_night_band_thresholds_apply(self) -> None:
        # 23:00 UTC is night in our default 21:00-06:00 wrap window
        # (assuming UTC=local for the test).
        now = datetime(2026, 5, 27, 23, 0, tzinfo=UTC)
        clock = FakeClock(now)
        reading = _reading(
            five_h_pct=65,  # below night.stop=90, but above day.stop=60
            weekly_pct=20,
            five_h_resets_at=now + timedelta(hours=2),
            weekly_resets_at=now + timedelta(days=4),
        )
        # Force timezone="UTC" to make the local time deterministic.
        d = decide(_policy(timezone="UTC"), reading, clock, poll_interval_s=POLL)
        assert d.state is SupervisorState.DISPATCHING
        assert d.band == "night"
        assert d.target_concurrency == 5


class TestThrottledWeekly:
    def test_observed_above_target_returns_throttled_weekly(self) -> None:
        # Place now at 50% elapsed; default early_pct=60, eow_switch=40h.
        # At t=0.5 with breakpoint 1-40/168 ≈ 0.762: target ≈ 0.5/0.762 * 60 ≈ 39.4%.
        # Observed = 60 → over target → throttled.
        now = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
        clock = FakeClock(now)
        weekly_resets_at = now + timedelta(seconds=SEVEN_DAYS_S / 2)
        reading = _reading(
            five_h_pct=5,
            weekly_pct=60,
            five_h_resets_at=now + timedelta(hours=2),
            weekly_resets_at=weekly_resets_at,
        )
        d = decide(_policy(timezone="UTC"), reading, clock, poll_interval_s=POLL)
        assert d.state is SupervisorState.THROTTLED_WEEKLY
        assert d.target_concurrency == 0
        assert d.target_pct is not None
        assert d.target_pct < 60.0

    def test_wakeup_clamped_to_next_5h_reset(self) -> None:
        now = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
        clock = FakeClock(now)
        five_h_reset_at = now + timedelta(minutes=15)
        weekly_resets_at = now + timedelta(seconds=SEVEN_DAYS_S / 2)
        reading = _reading(
            five_h_pct=5,
            weekly_pct=80,  # well above target at t=0.5
            five_h_resets_at=five_h_reset_at,
            weekly_resets_at=weekly_resets_at,
        )
        d = decide(_policy(timezone="UTC"), reading, clock, poll_interval_s=POLL)
        assert d.state is SupervisorState.THROTTLED_WEEKLY
        # Catch-up time would be far in the future; should be clamped to the
        # 5h reset.
        assert d.wakeup_at == five_h_reset_at

    def test_wakeup_clamped_above_now_plus_poll(self) -> None:
        # When the 5h reset is already in the past (stale reading) and
        # weekly is throttled, the inner min() could produce a wakeup
        # in the past; the max() guard clamps to now + poll_interval_s
        # so the supervisor doesn't busy-spin.
        now = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
        clock = FakeClock(now)
        weekly_resets_at = now + timedelta(seconds=SEVEN_DAYS_S / 2)
        # Stale 5h reset: 15 minutes ago.
        five_h_reset_at = now - timedelta(minutes=15)
        reading = _reading(
            five_h_pct=5,
            weekly_pct=60,  # above target at t=0.5 (~39%)
            five_h_resets_at=five_h_reset_at,
            weekly_resets_at=weekly_resets_at,
        )
        d = decide(_policy(timezone="UTC"), reading, clock, poll_interval_s=POLL)
        assert d.state is SupervisorState.THROTTLED_WEEKLY
        assert d.wakeup_at is not None
        assert d.wakeup_at >= now + timedelta(seconds=POLL)


class TestWeeklyResetUnparseable:
    def test_missing_weekly_resets_at_allows_dispatch(self) -> None:
        # When resets_at is None the curve has nothing to anchor to;
        # weekly is treated as "allow dispatch" so the 5h side gets to
        # classify normally.
        now = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
        clock = FakeClock(now)
        reading = _reading(
            five_h_pct=10,
            weekly_pct=95,  # would be over target if curve was anchored
            five_h_resets_at=now + timedelta(hours=2),
            weekly_resets_at=None,
        )
        d = decide(_policy(timezone="UTC"), reading, clock, poll_interval_s=POLL)
        assert d.state is SupervisorState.DISPATCHING
        assert d.target_pct is None


class TestInFlightSurvives:
    """``target_concurrency`` only gates new dispatches; the result
    carries no signal to kill in-flight tasks."""

    def test_throttled_5h_does_not_mention_in_flight(self) -> None:
        now = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
        clock = FakeClock(now)
        reading = _reading(
            five_h_pct=60,
            weekly_pct=20,
            five_h_resets_at=now + timedelta(hours=2),
            weekly_resets_at=now + timedelta(days=4),
        )
        d = decide(_policy(), reading, clock, poll_interval_s=POLL)
        assert d.state is SupervisorState.THROTTLED_5H
        # The Decision carries a 0 target; the supervisor's translation
        # layer is responsible for choosing not to spawn but keeping
        # in-flight alive.


class TestEventDiagnostics:
    """The Decision exposes the values needed for event payloads."""

    def test_exposes_target_pct_in_throttled_weekly(self) -> None:
        now = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
        clock = FakeClock(now)
        reading = _reading(
            five_h_pct=5,
            weekly_pct=70,
            five_h_resets_at=now + timedelta(hours=2),
            weekly_resets_at=now + timedelta(seconds=SEVEN_DAYS_S / 2),
        )
        d = decide(_policy(timezone="UTC"), reading, clock, poll_interval_s=POLL)
        assert d.state is SupervisorState.THROTTLED_WEEKLY
        assert d.target_pct is not None
        assert d.observed_weekly_pct == 70
        assert d.observed_5h_pct == 5

    def test_exposes_band_and_thresholds(self) -> None:
        now = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
        clock = FakeClock(now)
        reading = _reading(
            five_h_pct=10,
            weekly_pct=10,
            five_h_resets_at=now + timedelta(hours=2),
            weekly_resets_at=now + timedelta(days=4),
        )
        d = decide(_policy(timezone="UTC"), reading, clock, poll_interval_s=POLL)
        assert d.band == "day"
        assert d.fivehr_slowdown_pct == 40
        assert d.fivehr_stop_pct == 60
