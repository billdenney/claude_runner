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
from claude_task_runner.throttle.decision import (
    Decision,
    decide,
    end_of_week_opening,
    weekly_catch_up,
)
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
    five_h_pct: int | None,
    weekly_pct: int,
    five_h_resets_at: datetime | None,
    weekly_resets_at: datetime | None,
    captured_at: datetime | None = None,
) -> UsageReading:
    """``five_h_pct=None`` is a reading with no 5-hour window."""
    captured = captured_at or datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
    return UsageReading(
        captured_at=captured,
        five_hour=(
            None
            if five_h_pct is None
            else WindowReading(
                utilization_pct=five_h_pct,
                resets_at_raw="x",
                resets_at=five_h_resets_at,
            )
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
        d = decide(
            _policy(max_concurrency=5),
            reading,
            clock,
            poll_interval_s=POLL,
            window_start_delay_s=450,
        )
        assert d.state is SupervisorState.SLOWING_DOWN
        # Linear ramp: progress = (50-40)/(60-40) = 0.5; ceil(5 * 0.5) = 3.
        assert d.target_concurrency == 3
        # Just past the 5h reset: the reset plus the delay. 450s is
        # neither POLL nor the 300s default.
        assert d.wakeup_at == now + timedelta(hours=2, seconds=450)

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
        assert d.wakeup_at == now + timedelta(seconds=POLL)


class TestWeeklyCatchUp:
    """``weekly_catch_up``: when the curve meets a weekly-throttled reading.
    Status commands report it, so it must be the time decide() aims at."""

    def test_known_answer_on_the_default_curve(self) -> None:
        # 30% on the early segment (60% at the 40h elbow of a 168h week):
        # t = 30/60 * 128/168 = 64/168, so the curve meets it 104h before
        # the reset.
        resets = datetime(2026, 10, 21, 11, 0, tzinfo=UTC)
        got = weekly_catch_up(_policy(), observed_pct=30.0, resets_at=resets)
        assert got == resets - timedelta(hours=104)

    def test_at_or_above_eow_pct_is_the_reset(self) -> None:
        resets = datetime(2026, 10, 21, 11, 0, tzinfo=UTC)
        assert weekly_catch_up(_policy(), observed_pct=95.0, resets_at=resets) == resets

    def test_decide_wakes_at_the_catch_up_when_it_comes_first(self) -> None:
        now = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
        weekly_resets_at = now + timedelta(seconds=SEVEN_DAYS_S / 2)
        reading = _reading(
            five_h_pct=5,
            weekly_pct=45,
            five_h_resets_at=now + timedelta(days=3),
            weekly_resets_at=weekly_resets_at,
        )
        policy = _policy(timezone="UTC")
        d = decide(policy, reading, FakeClock(now), poll_interval_s=POLL)
        assert d.state is SupervisorState.THROTTLED_WEEKLY
        assert d.wakeup_at == weekly_catch_up(policy, observed_pct=45.0, resets_at=weekly_resets_at)
        assert d.wakeup_at is not None and now < d.wakeup_at < weekly_resets_at

    def test_known_answer_on_a_fourteen_day_window(self) -> None:
        # The 40h elbow is 40/336 of a 336h window, so t_elbow = 296/336 and
        # 30% is met at t = 30/60 * 296/336 = 148/336: 188h before the reset.
        resets = datetime(2026, 10, 21, 11, 0, tzinfo=UTC)
        got = weekly_catch_up(
            _policy(), observed_pct=30.0, resets_at=resets, weekly_window_s=2 * SEVEN_DAYS_S
        )
        assert got == resets - timedelta(hours=188)

    def test_decide_uses_its_own_window(self) -> None:
        """On a 14-day window, 45% half-way through is over the curve, which
        meets it 114h before the reset (t = 45/60 * 296/336 = 222/336); the
        seven-day curve would say 72h. The 5-hour reset is later than both,
        so it does not clamp the wakeup."""
        now = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
        weekly_resets_at = now + timedelta(days=7)
        reading = _reading(
            five_h_pct=5,
            weekly_pct=45,
            five_h_resets_at=now + timedelta(days=9),
            weekly_resets_at=weekly_resets_at,
        )
        d = decide(
            _policy(timezone="UTC"),
            reading,
            FakeClock(now),
            poll_interval_s=POLL,
            weekly_window_s=2 * SEVEN_DAYS_S,
        )
        assert d.state is SupervisorState.THROTTLED_WEEKLY
        assert d.wakeup_at == weekly_resets_at - timedelta(hours=114)


class TestNoFiveHourWindow:
    """A reading with no 5-hour window (ADR-0038): the 5h thresholds do not
    apply, and the weekly trace decides alone."""

    NOW = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
    WEEKLY_RESETS_AT = NOW + timedelta(seconds=SEVEN_DAYS_S / 2)
    """Half-way through the week: the default curve's target is 30/0.762 ~ 39.4%."""

    def _decide(self, weekly_pct: int, *, day_slow: int = 40, day_stop: int = 60) -> Decision:
        reading = _reading(
            five_h_pct=None,
            weekly_pct=weekly_pct,
            five_h_resets_at=None,
            weekly_resets_at=self.WEEKLY_RESETS_AT,
        )
        return decide(
            _policy(timezone="UTC", day_slow=day_slow, day_stop=day_stop),
            reading,
            FakeClock(self.NOW),
            poll_interval_s=POLL,
            window_start_delay_s=450,
        )

    def test_weekly_under_target_dispatches_at_max_concurrency(self) -> None:
        d = self._decide(weekly_pct=12)
        assert d.state is SupervisorState.DISPATCHING
        assert d.target_concurrency == 5
        assert d.wakeup_at is None
        assert d.message == ""
        assert d.observed_5h_pct is None
        assert d.observed_weekly_pct == 12
        assert d.target_pct is not None and 39.0 < d.target_pct < 40.0

    def test_weekly_over_target_throttles_until_the_catch_up(self) -> None:
        """No 5h reset clamps the wakeup: it is the catch-up time itself."""
        d = self._decide(weekly_pct=45)
        assert d.state is SupervisorState.THROTTLED_WEEKLY
        assert d.target_concurrency == 0
        assert d.observed_5h_pct is None
        assert d.observed_weekly_pct == 45
        assert d.wakeup_at == weekly_catch_up(
            _policy(timezone="UTC"), observed_pct=45.0, resets_at=self.WEEKLY_RESETS_AT
        )
        # 45% is met at t = 45/60 * 128/168 = 96/168, 72h before the reset.
        assert d.wakeup_at == self.WEEKLY_RESETS_AT - timedelta(hours=72)

    def test_five_hour_thresholds_of_zero_do_not_apply(self) -> None:
        """A missing window is not 0%: thresholds at 0 would throttle a 0%
        reading, and do not touch one with no 5-hour window."""
        d = self._decide(weekly_pct=12, day_slow=0, day_stop=0)
        assert d.state is SupervisorState.DISPATCHING
        assert d.target_concurrency == 5
        assert d.fivehr_slowdown_pct == 0
        assert d.fivehr_stop_pct == 0
        assert d.band == "day"

    def test_zero_percent_five_hour_window_does_meet_zero_thresholds(self) -> None:
        """The control for the test above: the same thresholds stop a reading
        whose 5-hour window is reported at 0%."""
        reading = _reading(
            five_h_pct=0,
            weekly_pct=12,
            five_h_resets_at=self.NOW + timedelta(hours=2),
            weekly_resets_at=self.WEEKLY_RESETS_AT,
        )
        d = decide(
            _policy(timezone="UTC", day_slow=0, day_stop=0),
            reading,
            FakeClock(self.NOW),
            poll_interval_s=POLL,
        )
        assert d.state is SupervisorState.THROTTLED_5H
        assert d.observed_5h_pct == 0

    def test_no_weekly_reset_and_no_five_hour_window_dispatches(self) -> None:
        reading = _reading(
            five_h_pct=None, weekly_pct=99, five_h_resets_at=None, weekly_resets_at=None
        )
        d = decide(_policy(timezone="UTC"), reading, FakeClock(self.NOW), poll_interval_s=POLL)
        assert d.state is SupervisorState.DISPATCHING
        assert d.target_concurrency == 5
        assert d.target_pct is None
        assert d.observed_5h_pct is None


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


class TestDefensiveBranches:
    """Edge-case defensive branches inside :mod:`throttle.decision`.

    These exercise rare but reachable inputs that the schema layer
    does not currently reject (parsing failure on the 5h reset
    timestamp; a misconfigured slowdown_pct >= stop_pct). Kept here so
    the coverage gate pins them.
    """

    def test_5h_reset_unparseable_falls_back_to_fixed_delay(self) -> None:
        """When the OAuth reading has no ``resets_at`` the wakeup math
        falls back to ``now + FIVE_HOUR_LENGTH_S + delay``."""
        now = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
        clock = FakeClock(now)
        reading = _reading(
            five_h_pct=65,  # in 5h stop band (day=60)
            weekly_pct=10,
            five_h_resets_at=None,  # parser failure
            weekly_resets_at=now + timedelta(days=4),
        )
        d = decide(
            _policy(timezone="UTC"),
            reading,
            clock,
            poll_interval_s=POLL,
            window_start_delay_s=450,
        )
        assert d.state is SupervisorState.THROTTLED_5H
        # Exactly one 5h window plus the delay. 450s is neither POLL nor
        # the 300s default, so a dropped, doubled or hardcoded delay
        # term cannot land on this time.
        assert d.wakeup_at == now + timedelta(hours=5, seconds=450)

    def test_degenerate_slow_equals_stop_returns_zero(self) -> None:
        """A misconfigured ResolvedBand with slowdown_pct >= stop_pct
        cannot ramp; the linear-ramp helper returns 0 instead of
        dividing by zero."""
        now = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
        clock = FakeClock(now)
        # slow = stop = 50 — zero-span band.
        reading = _reading(
            five_h_pct=50,  # exactly at the zero-span band
            weekly_pct=10,
            five_h_resets_at=now + timedelta(hours=2),
            weekly_resets_at=now + timedelta(days=4),
        )
        d = decide(
            _policy(day_slow=50, day_stop=50, timezone="UTC"),
            reading,
            clock,
            poll_interval_s=POLL,
        )
        # observed >= slowdown=50 AND observed >= stop=50 → THROTTLED_5H.
        # _linear_ramp isn't called on the stop path, but it's exercised
        # when observed is in the (slowdown, stop) span. Verify the
        # zero-span pretends to be stop:
        assert d.state is SupervisorState.THROTTLED_5H
        assert d.target_concurrency == 0

    def test_degenerate_slow_above_stop_returns_zero(self) -> None:
        """slowdown_pct >= stop_pct is illegal but defended-against in
        the linear-ramp helper. The natural decision path never enters
        ``_linear_ramp`` with an inverted/zero span (band classification
        kicks in first), so the branch is covered directly here.
        """
        from claude_task_runner.throttle.decision import _linear_ramp

        out = _linear_ramp(observed_pct=65, slowdown_pct=70, stop_pct=60, max_concurrency=5)
        assert out == 0


class TestEndOfWeekOnly:
    """``early_pct = 0`` (ADR-0041): an account takes tasks only in the last
    ``eow_time_switch`` before its weekly reset, up to ``eow_pct``.

    The policy mirrors an account with no 5-hour window kept for the end of
    the week: early 0, eow 97, a 12h window, five tasks at once.
    """

    RESETS = datetime(2026, 11, 11, 11, 0, tzinfo=UTC)
    OPENS = RESETS - timedelta(hours=12)

    def _decide(
        self,
        now: datetime,
        weekly_pct: int,
        *,
        eow_switch_s: float = 12 * 3600,
        five_h_pct: int | None = None,
        five_h_resets_at: datetime | None = None,
    ) -> Decision:
        policy = _policy(early_pct=0, eow_pct=97, eow_switch_s=eow_switch_s, timezone="UTC")
        reading = _reading(
            five_h_pct=five_h_pct,
            weekly_pct=weekly_pct,
            five_h_resets_at=five_h_resets_at,
            weekly_resets_at=self.RESETS,
        )
        return decide(policy, reading, FakeClock(now), poll_interval_s=POLL)

    def test_before_the_window_it_waits_for_the_opening(self) -> None:
        d = self._decide(self.OPENS - timedelta(hours=8), weekly_pct=68)
        assert d.state is SupervisorState.THROTTLED_WEEKLY
        assert d.target_concurrency == 0
        assert d.target_pct == 0.0
        assert d.wakeup_at == self.OPENS
        assert d.message == (
            "early_pct = 0: waiting for the end-of-week window, the last 12h before "
            "the weekly reset, which opens at 2026-11-10T23:00+00:00"
        )

    def test_a_fresh_week_at_zero_percent_does_not_dispatch(self) -> None:
        # Observed 0% is not above a 0% target, so only the closed window
        # keeps the account from starting tasks the moment its week resets.
        d = self._decide(self.RESETS - timedelta(days=7), weekly_pct=0)
        assert d.state is SupervisorState.THROTTLED_WEEKLY
        assert d.target_concurrency == 0
        assert d.wakeup_at == self.OPENS

    def test_the_window_opens_on_time(self) -> None:
        d = self._decide(self.OPENS, weekly_pct=75)
        assert d.state is SupervisorState.DISPATCHING
        assert d.target_concurrency == 5
        assert d.target_pct == 97.0

    def test_inside_the_window_it_runs_up_to_eow_pct(self) -> None:
        d = self._decide(self.OPENS + timedelta(hours=6), weekly_pct=97)
        assert d.state is SupervisorState.DISPATCHING
        assert d.target_concurrency == 5

    NEXT_OPENS = OPENS + timedelta(days=7)
    """Next week's window: after the reset this week's use no longer counts,
    but the new week's window stays closed until 12h before its reset."""

    def test_inside_the_window_above_eow_pct_it_waits_for_next_weeks_window(self) -> None:
        now = self.OPENS + timedelta(hours=6)
        d = self._decide(now, weekly_pct=98)
        assert d.state is SupervisorState.THROTTLED_WEEKLY
        assert d.wakeup_at == self.NEXT_OPENS
        assert d.message == (
            "early_pct = 0: weekly utilization 98% > eow_pct 97%, so this account takes no "
            "more tasks this week; the next end-of-week window opens at 2026-11-17T23:00+00:00"
        )

    def test_before_the_window_above_eow_pct_it_waits_for_next_weeks_window(self) -> None:
        d = self._decide(self.OPENS - timedelta(hours=1), weekly_pct=99)
        assert d.state is SupervisorState.THROTTLED_WEEKLY
        assert d.wakeup_at == self.NEXT_OPENS
        assert d.message.startswith("early_pct = 0: weekly utilization 99% > eow_pct 97%")

    def test_a_five_hour_reset_before_the_opening_clamps_the_wakeup(self) -> None:
        now = self.OPENS - timedelta(hours=8)
        five_h_reset = now + timedelta(hours=2)
        d = self._decide(now, weekly_pct=20, five_h_pct=10, five_h_resets_at=five_h_reset)
        assert d.state is SupervisorState.THROTTLED_WEEKLY
        assert d.wakeup_at == five_h_reset

    def test_inside_the_window_the_5h_bands_still_apply(self) -> None:
        now = datetime(2026, 11, 11, 3, 0, tzinfo=UTC)  # night band, inside the window
        d = self._decide(
            now, weekly_pct=50, five_h_pct=95, five_h_resets_at=now + timedelta(hours=1)
        )
        assert d.state is SupervisorState.THROTTLED_5H

    def test_a_zero_length_window_never_opens(self) -> None:
        d = self._decide(self.OPENS, weekly_pct=0, eow_switch_s=0)
        assert d.state is SupervisorState.THROTTLED_WEEKLY
        assert d.wakeup_at == self.RESETS
        assert d.message == (
            "early_pct = 0 takes tasks only in the last eow_time_switch before the weekly "
            "reset, and eow_time_switch is 0, so this account takes none"
        )

    def test_weekly_catch_up_is_the_opening(self) -> None:
        policy = _policy(early_pct=0, eow_pct=97, eow_switch_s=12 * 3600)
        assert weekly_catch_up(policy, observed_pct=68.0, resets_at=self.RESETS) == self.OPENS
        assert weekly_catch_up(policy, observed_pct=97.0, resets_at=self.RESETS) == self.OPENS
        assert weekly_catch_up(policy, observed_pct=98.0, resets_at=self.RESETS) == (
            self.NEXT_OPENS
        )

    def test_end_of_week_opening_is_none_unless_the_window_opens_and_closes(self) -> None:
        def opening(early: int, switch_s: float, observed: float = 50.0) -> datetime | None:
            policy = _policy(early_pct=early, eow_pct=97, eow_switch_s=switch_s)
            return end_of_week_opening(policy, observed_pct=observed, resets_at=self.RESETS)

        assert opening(0, 12 * 3600) == self.OPENS
        assert opening(0, 12 * 3600, observed=97.0) == self.OPENS
        assert opening(0, 12 * 3600, observed=97.5) == self.NEXT_OPENS
        assert opening(60, 12 * 3600) is None
        assert opening(0, 0) is None
        assert opening(0, SEVEN_DAYS_S) is None
        assert opening(0, 2 * SEVEN_DAYS_S) is None

    def test_at_eow_pct_before_the_window_it_wakes_at_the_opening_and_dispatches(self) -> None:
        before = self._decide(self.OPENS - timedelta(hours=1), weekly_pct=97)
        assert before.state is SupervisorState.THROTTLED_WEEKLY
        assert before.wakeup_at == self.OPENS
        at_opening = self._decide(self.OPENS, weekly_pct=97)
        assert at_opening.state is SupervisorState.DISPATCHING

    def test_a_reading_past_its_reset_keeps_the_window_closed(self) -> None:
        # The last reading of a week stays current for up to a poll after the
        # reset; the new week's window is closed, so 0% then must not dispatch.
        now = self.RESETS + timedelta(minutes=3)
        d = self._decide(now, weekly_pct=10)
        assert d.state is SupervisorState.THROTTLED_WEEKLY
        assert d.target_concurrency == 0
        assert d.target_pct == 0.0
        assert d.wakeup_at == now + timedelta(seconds=POLL)
        assert d.message == (
            "early_pct = 0: the weekly reset at 2026-11-11T11:00+00:00 has passed, so the "
            "end-of-week window is closed until a reading of the new week shows when it opens"
        )

    def test_a_window_of_a_week_or_more_is_always_open(self) -> None:
        for switch_s in (SEVEN_DAYS_S, 2 * SEVEN_DAYS_S):
            mid_week = self._decide(self.RESETS - timedelta(days=4), 0, eow_switch_s=switch_s)
            assert mid_week.state is SupervisorState.DISPATCHING
            assert mid_week.target_pct == 97.0
            over = self._decide(self.RESETS - timedelta(days=4), 98, eow_switch_s=switch_s)
            assert over.state is SupervisorState.THROTTLED_WEEKLY
            assert over.wakeup_at == self.RESETS

    def test_a_fourteen_day_window_opens_12h_before_its_reset(self) -> None:
        policy = _policy(early_pct=0, eow_pct=97, eow_switch_s=12 * 3600, timezone="UTC")

        def at(now: datetime) -> Decision:
            reading = _reading(
                five_h_pct=None, weekly_pct=40, five_h_resets_at=None, weekly_resets_at=self.RESETS
            )
            return decide(
                policy,
                reading,
                FakeClock(now),
                poll_interval_s=POLL,
                weekly_window_s=2 * SEVEN_DAYS_S,
            )

        before = at(self.RESETS - timedelta(days=10))
        assert before.state is SupervisorState.THROTTLED_WEEKLY
        assert before.wakeup_at == self.OPENS
        assert at(self.OPENS).state is SupervisorState.DISPATCHING

    def test_the_reported_opening_dispatches_for_every_whole_second_switch(self) -> None:
        """eow_time_switch is whole seconds (config.duration); readings carry
        microseconds at most. At the opening weekly_catch_up reports, decide()
        must already dispatch, or the account starts a poll late."""
        for window_s in (SEVEN_DAYS_S, 2 * SEVEN_DAYS_S):
            for switch_s in (1, 59, 3600, 12 * 3600, 12 * 3600 + 1, 40 * 3600, 86399, 518401):
                policy = _policy(early_pct=0, eow_pct=97, eow_switch_s=float(switch_s))
                for micro in (0, 1, 123456, 999999):
                    resets = self.RESETS + timedelta(microseconds=micro)
                    opens = weekly_catch_up(
                        policy, observed_pct=50.0, resets_at=resets, weekly_window_s=window_s
                    )
                    reading = _reading(
                        five_h_pct=None,
                        weekly_pct=50,
                        five_h_resets_at=None,
                        weekly_resets_at=resets,
                    )
                    d = decide(
                        policy,
                        reading,
                        FakeClock(opens),
                        poll_interval_s=POLL,
                        weekly_window_s=window_s,
                    )
                    assert d.state is SupervisorState.DISPATCHING, (window_s, switch_s, micro)

    def test_a_sloped_curve_still_dispatches_at_zero_percent_at_week_start(self) -> None:
        reading = _reading(
            five_h_pct=None,
            weekly_pct=0,
            five_h_resets_at=None,
            weekly_resets_at=self.RESETS,
        )
        now = self.RESETS - timedelta(days=7)
        d = decide(_policy(timezone="UTC"), reading, FakeClock(now), poll_interval_s=POLL)
        assert d.state is SupervisorState.DISPATCHING
