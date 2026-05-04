"""Tests for supervisor.window — window math and reset detection."""

from __future__ import annotations

from datetime import UTC, datetime

from claude_task_runner.clock import FakeClock
from claude_task_runner.supervisor.window import (
    FIVE_HOUR_LENGTH_S,
    SEVEN_DAY_LENGTH_S,
    crossed_reset,
    crossed_reset_5h,
    crossed_reset_weekly,
    in_eow_push_window,
    schedule_window_start_wakeup,
    time_until_reset_s,
)
from claude_task_runner.usage.models import UsageReading, WindowReading


def _w(pct: int, resets_at: datetime | None) -> WindowReading:
    return WindowReading(
        utilization_pct=pct,
        resets_at_raw="placeholder",
        resets_at=resets_at,
    )


def _reading(*, five: WindowReading, weekly: WindowReading) -> UsageReading:
    return UsageReading(
        captured_at=datetime(2026, 5, 4, 12, 0, tzinfo=UTC),
        five_hour=five,
        seven_day=weekly,
    )


class TestTimeUntilReset:
    def test_in_future(self) -> None:
        clock = FakeClock(datetime(2026, 5, 4, 12, 0, tzinfo=UTC))
        w = _w(50, datetime(2026, 5, 4, 13, 30, tzinfo=UTC))
        assert time_until_reset_s(w, clock=clock, fallback_window_length_s=18000) == 5400.0

    def test_in_past_clamps_zero(self) -> None:
        clock = FakeClock(datetime(2026, 5, 4, 14, 0, tzinfo=UTC))
        w = _w(50, datetime(2026, 5, 4, 13, 30, tzinfo=UTC))
        assert time_until_reset_s(w, clock=clock, fallback_window_length_s=18000) == 0.0

    def test_no_resets_at_uses_fallback(self) -> None:
        clock = FakeClock(datetime(2026, 5, 4, 12, 0, tzinfo=UTC))
        w = _w(50, None)
        assert time_until_reset_s(w, clock=clock, fallback_window_length_s=18000) == 18000.0


class TestCrossedReset:
    def _clock(self) -> FakeClock:
        return FakeClock(datetime(2026, 5, 4, 13, 30, tzinfo=UTC))

    def test_no_previous_returns_false(self) -> None:
        assert (
            crossed_reset(
                previous=None,
                current=_w(50, datetime(2026, 5, 4, 18, 0, tzinfo=UTC)),
                clock=self._clock(),
            )
            is False
        )

    def test_previous_in_past_returns_true(self) -> None:
        # previous resets target was 13:00, now is 13:30 → crossed.
        prev = _w(80, datetime(2026, 5, 4, 13, 0, tzinfo=UTC))
        curr = _w(5, datetime(2026, 5, 4, 18, 0, tzinfo=UTC))
        assert crossed_reset(previous=prev, current=curr, clock=self._clock()) is True

    def test_jumped_forward_returns_true(self) -> None:
        # Previous reset was 18:00, current jumped to 23:00 (5h later).
        prev = _w(80, datetime(2026, 5, 4, 18, 0, tzinfo=UTC))
        curr = _w(5, datetime(2026, 5, 4, 23, 0, tzinfo=UTC))
        assert crossed_reset(previous=prev, current=curr, clock=self._clock()) is True

    def test_no_change_returns_false(self) -> None:
        prev = _w(50, datetime(2026, 5, 4, 18, 0, tzinfo=UTC))
        curr = _w(60, datetime(2026, 5, 4, 18, 0, tzinfo=UTC))
        assert crossed_reset(previous=prev, current=curr, clock=self._clock()) is False

    def test_grace_window(self) -> None:
        # Previous reset was 13:29:30, now is 13:30. Within 60s grace,
        # so we report crossed.
        prev = _w(80, datetime(2026, 5, 4, 13, 29, 30, tzinfo=UTC))
        curr = _w(5, datetime(2026, 5, 4, 18, 30, tzinfo=UTC))
        assert crossed_reset(previous=prev, current=curr, clock=self._clock(), grace_s=60.0) is True

    def test_resets_at_none_returns_false(self) -> None:
        prev = _w(80, None)
        curr = _w(5, datetime(2026, 5, 4, 18, 0, tzinfo=UTC))
        assert crossed_reset(previous=prev, current=curr, clock=self._clock()) is False


class TestCrossedResetWrappers:
    def test_5h_passes_through(self) -> None:
        clock = FakeClock(datetime(2026, 5, 4, 13, 30, tzinfo=UTC))
        prev_r = _reading(
            five=_w(80, datetime(2026, 5, 4, 13, 0, tzinfo=UTC)),
            weekly=_w(20, datetime(2026, 5, 8, 3, 0, tzinfo=UTC)),
        )
        curr_r = _reading(
            five=_w(5, datetime(2026, 5, 4, 18, 0, tzinfo=UTC)),
            weekly=_w(20, datetime(2026, 5, 8, 3, 0, tzinfo=UTC)),
        )
        assert crossed_reset_5h(previous=prev_r, current=curr_r, clock=clock) is True
        assert crossed_reset_weekly(previous=prev_r, current=curr_r, clock=clock) is False


class TestInEowPushWindow:
    def test_inside_window(self) -> None:
        clock = FakeClock(datetime(2026, 5, 4, 12, 0, tzinfo=UTC))
        w = _w(95, datetime(2026, 5, 4, 18, 0, tzinfo=UTC))
        # 6h until reset, eow window is 12h → inside
        assert in_eow_push_window(weekly=w, clock=clock, eow_window_s=43200) is True

    def test_outside_window(self) -> None:
        clock = FakeClock(datetime(2026, 5, 4, 12, 0, tzinfo=UTC))
        w = _w(95, datetime(2026, 5, 6, 12, 0, tzinfo=UTC))  # 48h away
        assert in_eow_push_window(weekly=w, clock=clock, eow_window_s=43200) is False

    def test_no_resets_at_returns_false(self) -> None:
        clock = FakeClock(datetime(2026, 5, 4, 12, 0, tzinfo=UTC))
        assert in_eow_push_window(weekly=_w(95, None), clock=clock, eow_window_s=43200) is False

    def test_already_past_resets_at_returns_false(self) -> None:
        clock = FakeClock(datetime(2026, 5, 4, 12, 0, tzinfo=UTC))
        w = _w(95, datetime(2026, 5, 4, 11, 0, tzinfo=UTC))  # 1h ago
        assert in_eow_push_window(weekly=w, clock=clock, eow_window_s=43200) is False


class TestScheduleWakeup:
    def test_uses_resets_at_plus_delay(self) -> None:
        clock = FakeClock(datetime(2026, 5, 4, 12, 0, tzinfo=UTC))
        w = _w(80, datetime(2026, 5, 4, 17, 0, tzinfo=UTC))
        result = schedule_window_start_wakeup(
            window=w,
            clock=clock,
            delay_s=300,
            fallback_window_length_s=18000,
        )
        assert result == datetime(2026, 5, 4, 17, 5, tzinfo=UTC)

    def test_falls_back_when_no_resets_at(self) -> None:
        clock = FakeClock(datetime(2026, 5, 4, 12, 0, tzinfo=UTC))
        w = _w(80, None)
        result = schedule_window_start_wakeup(
            window=w,
            clock=clock,
            delay_s=300,
            fallback_window_length_s=18000,
        )
        # 12:00 + 18000s + 300s = 17:05
        assert result == datetime(2026, 5, 4, 17, 5, tzinfo=UTC)


class TestConstants:
    def test_lengths_match_documentation(self) -> None:
        assert FIVE_HOUR_LENGTH_S == 5 * 3600
        assert SEVEN_DAY_LENGTH_S == 7 * 24 * 3600
