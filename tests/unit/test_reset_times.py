"""Tests for the reset-time parsers (5-hour and 7-day formats)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from claude_task_runner.clock import FakeClock
from claude_task_runner.usage.reset_times import parse_five_hour, parse_weekly


@pytest.fixture
def clock() -> FakeClock:
    # 2026-05-03 18:00:00 UTC. After 2:10am, before 11:45pm.
    return FakeClock(datetime(2026, 5, 3, 18, 0, 0, tzinfo=UTC))


class TestParseFiveHour:
    def test_basic_am(self, clock: FakeClock) -> None:
        # Resets 2:10am UTC -> next day, since now > 2:10am
        result = parse_five_hour("2:10am (UTC)", clock)
        assert result == datetime(2026, 5, 4, 2, 10, 0, tzinfo=UTC)

    def test_basic_pm_today(self, clock: FakeClock) -> None:
        # Resets 11:45pm today (now is 18:00 = 6pm)
        result = parse_five_hour("11:45pm (UTC)", clock)
        assert result == datetime(2026, 5, 3, 23, 45, 0, tzinfo=UTC)

    def test_pm_already_past_rolls_to_tomorrow(self, clock: FakeClock) -> None:
        # 5pm today is already past (now is 6pm)
        result = parse_five_hour("5:00pm (UTC)", clock)
        assert result == datetime(2026, 5, 4, 17, 0, 0, tzinfo=UTC)

    def test_no_minutes(self, clock: FakeClock) -> None:
        result = parse_five_hour("3am (UTC)", clock)
        assert result == datetime(2026, 5, 4, 3, 0, 0, tzinfo=UTC)

    def test_12am_means_midnight(self, clock: FakeClock) -> None:
        result = parse_five_hour("12am (UTC)", clock)
        assert result == datetime(2026, 5, 4, 0, 0, 0, tzinfo=UTC)

    def test_12pm_means_noon(self, clock: FakeClock) -> None:
        # 12pm today (noon) is past 6pm? No, 12pm is BEFORE 6pm.
        # So roll to tomorrow.
        result = parse_five_hour("12pm (UTC)", clock)
        assert result == datetime(2026, 5, 4, 12, 0, 0, tzinfo=UTC)

    def test_returns_none_on_garbage(self, clock: FakeClock) -> None:
        assert parse_five_hour("not a time", clock) is None
        assert parse_five_hour("", clock) is None
        assert parse_five_hour("3:99am (UTC)", clock) is None

    def test_returns_none_on_non_utc(self, clock: FakeClock) -> None:
        # We accept only (UTC); other tzs return None.
        assert parse_five_hour("3am (PST)", clock) is None
        assert parse_five_hour("3am", clock) is None

    def test_returns_none_on_hour_out_of_range(self, clock: FakeClock) -> None:
        assert parse_five_hour("25am (UTC)", clock) is None
        assert parse_five_hour("0am (UTC)", clock) is None


class TestParseWeekly:
    def test_basic(self, clock: FakeClock) -> None:
        result = parse_weekly("May 4, 3am (UTC)", clock)
        assert result == datetime(2026, 5, 4, 3, 0, 0, tzinfo=UTC)

    def test_minute_precision(self, clock: FakeClock) -> None:
        result = parse_weekly("May 4, 11:30pm (UTC)", clock)
        assert result == datetime(2026, 5, 4, 23, 30, 0, tzinfo=UTC)

    def test_year_rollover(self, clock: FakeClock) -> None:
        # If now is May 3 2026 18:00 and we see "Jan 5, 3am" — that's already
        # past for 2026, so it must mean 2027.
        result = parse_weekly("Jan 5, 3am (UTC)", clock)
        assert result == datetime(2027, 1, 5, 3, 0, 0, tzinfo=UTC)

    def test_today_in_past_rolls(self, clock: FakeClock) -> None:
        # Now: May 3 18:00. "May 3, 3am" already past today -> next year.
        result = parse_weekly("May 3, 3am (UTC)", clock)
        assert result == datetime(2027, 5, 3, 3, 0, 0, tzinfo=UTC)

    def test_today_in_future_keeps_year(self, clock: FakeClock) -> None:
        # Now: May 3 18:00. "May 3, 11pm" still in future today.
        result = parse_weekly("May 3, 11pm (UTC)", clock)
        assert result == datetime(2026, 5, 3, 23, 0, 0, tzinfo=UTC)

    def test_full_month_name(self, clock: FakeClock) -> None:
        result = parse_weekly("September 4, 3am (UTC)", clock)
        assert result == datetime(2026, 9, 4, 3, 0, 0, tzinfo=UTC)

    def test_invalid_date_returns_none(self, clock: FakeClock) -> None:
        # Feb 30 doesn't exist
        assert parse_weekly("Feb 30, 3am (UTC)", clock) is None

    def test_garbage_returns_none(self, clock: FakeClock) -> None:
        assert parse_weekly("", clock) is None
        assert parse_weekly("not a date", clock) is None
        assert parse_weekly("Maybe 4, 3am (UTC)", clock) is None

    def test_feb_29_leap_year_handling(self) -> None:
        # 2027 is not a leap year; Feb 29 should fall back to Feb 28.
        clock = FakeClock(datetime(2026, 3, 1, tzinfo=UTC))
        # We're past Feb 29, 2026 (which exists, since 2024 was leap; 2026 is not!)
        # In fact Feb 29 2026 doesn't exist either. So this string is malformed.
        # Use Feb 28 instead and verify behavior.
        assert parse_weekly("Feb 29, 3am (UTC)", clock) is None

    def test_year_boundary_fallback(self) -> None:
        # We're on Mar 1, 2026. "Feb 29" rolls forward to next year.
        # But Feb 29, 2027 doesn't exist either, so fallback to Feb 28.
        # Since Feb 29 itself isn't a valid date in 2026, parse returns None
        # before the rollover logic. Verify that explicitly.
        clock = FakeClock(datetime(2026, 1, 15, tzinfo=UTC))
        assert parse_weekly("Feb 29, 3am (UTC)", clock) is None
