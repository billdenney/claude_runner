"""Tests for supervisor.time_of_day — pure functions, full coverage."""

from __future__ import annotations

import zoneinfo
from datetime import UTC, datetime, time, timedelta

import pytest

from claude_task_runner.supervisor.time_of_day import (
    DayNightBand,
    daytime_weight,
    effective_threshold,
    is_nighttime,
    parse_hhmm,
    to_local,
)

DAY_START = time(6, 0)
DAY_END = time(22, 0)
RAMP = 30  # minutes


def _local_at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    """Build a UTC-tagged datetime that yields the given local clock time.

    The functions under test only read hour/minute/second; the date and
    tzinfo are irrelevant to them, so we pin to a fixed reference date.
    """
    return datetime(2026, 5, 13, hour, minute, second, tzinfo=UTC)


class TestParseHHMM:
    def test_midnight(self) -> None:
        assert parse_hhmm("00:00") == time(0, 0)

    def test_noon(self) -> None:
        assert parse_hhmm("12:00") == time(12, 0)

    def test_end_of_day(self) -> None:
        assert parse_hhmm("23:59") == time(23, 59)

    def test_zero_padded_hour(self) -> None:
        assert parse_hhmm("06:30") == time(6, 30)

    @pytest.mark.parametrize("bad", ["", ":", "12", "12:", ":30", "abc"])
    def test_malformed_raises(self, bad: str) -> None:
        with pytest.raises(ValueError):
            parse_hhmm(bad)

    @pytest.mark.parametrize("bad", ["24:00", "12:60", "-1:00", "12:-5"])
    def test_out_of_range_raises(self, bad: str) -> None:
        with pytest.raises(ValueError):
            parse_hhmm(bad)


class TestToLocal:
    def test_naive_datetime_rejected(self) -> None:
        with pytest.raises(ValueError):
            to_local(datetime(2026, 5, 13, 12, 0, 0), tz_name="")

    def test_explicit_iana_tz(self) -> None:
        utc_noon = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
        ny = to_local(utc_noon, tz_name="America/New_York")
        # 2026-05-13 is in EDT (UTC-04:00), so 12:00 UTC = 08:00 EDT.
        assert ny.hour == 8

    def test_empty_tz_uses_system_local(self) -> None:
        utc_now = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
        local = to_local(utc_now, tz_name="")
        # Round-trip back to UTC must match.
        assert local.astimezone(UTC) == utc_now

    def test_invalid_tz_raises(self) -> None:
        with pytest.raises(zoneinfo.ZoneInfoNotFoundError):
            to_local(datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC), tz_name="Nowhere/Ville")


class TestDaytimeWeight:
    def test_core_day(self) -> None:
        assert (
            daytime_weight(
                _local_at(12, 0), day_start=DAY_START, day_end=DAY_END, ramp_minutes=RAMP
            )
            == 1.0
        )

    def test_core_night_after_midnight(self) -> None:
        assert (
            daytime_weight(_local_at(2, 0), day_start=DAY_START, day_end=DAY_END, ramp_minutes=RAMP)
            == 0.0
        )

    def test_core_night_late_evening(self) -> None:
        assert (
            daytime_weight(
                _local_at(23, 30), day_start=DAY_START, day_end=DAY_END, ramp_minutes=RAMP
            )
            == 0.0
        )

    def test_morning_ramp_midpoint(self) -> None:
        """At the boundary itself the weight is exactly 0.5."""
        assert daytime_weight(
            _local_at(6, 0), day_start=DAY_START, day_end=DAY_END, ramp_minutes=RAMP
        ) == pytest.approx(0.5)

    def test_morning_ramp_start(self) -> None:
        assert daytime_weight(
            _local_at(5, 45), day_start=DAY_START, day_end=DAY_END, ramp_minutes=RAMP
        ) == pytest.approx(0.0)

    def test_morning_ramp_end(self) -> None:
        assert daytime_weight(
            _local_at(6, 15), day_start=DAY_START, day_end=DAY_END, ramp_minutes=RAMP
        ) == pytest.approx(1.0)

    def test_evening_ramp_midpoint(self) -> None:
        assert daytime_weight(
            _local_at(22, 0), day_start=DAY_START, day_end=DAY_END, ramp_minutes=RAMP
        ) == pytest.approx(0.5)

    def test_evening_ramp_start(self) -> None:
        assert daytime_weight(
            _local_at(21, 45), day_start=DAY_START, day_end=DAY_END, ramp_minutes=RAMP
        ) == pytest.approx(1.0)

    def test_evening_ramp_end(self) -> None:
        assert daytime_weight(
            _local_at(22, 15), day_start=DAY_START, day_end=DAY_END, ramp_minutes=RAMP
        ) == pytest.approx(0.0)

    def test_zero_ramp_step_in_day(self) -> None:
        """``ramp_minutes = 0`` is a hard step function."""
        assert (
            daytime_weight(_local_at(12, 0), day_start=DAY_START, day_end=DAY_END, ramp_minutes=0)
            == 1.0
        )

    def test_zero_ramp_step_at_start_inclusive(self) -> None:
        assert (
            daytime_weight(_local_at(6, 0), day_start=DAY_START, day_end=DAY_END, ramp_minutes=0)
            == 1.0
        )

    def test_zero_ramp_step_at_end_exclusive(self) -> None:
        assert (
            daytime_weight(_local_at(22, 0), day_start=DAY_START, day_end=DAY_END, ramp_minutes=0)
            == 0.0
        )

    def test_zero_ramp_step_in_night(self) -> None:
        assert (
            daytime_weight(_local_at(3, 0), day_start=DAY_START, day_end=DAY_END, ramp_minutes=0)
            == 0.0
        )

    def test_zero_length_day_always_night(self) -> None:
        """``day_start == day_end`` is treated as always-nighttime."""
        assert (
            daytime_weight(
                _local_at(12, 0),
                day_start=time(6, 0),
                day_end=time(6, 0),
                ramp_minutes=30,
            )
            == 0.0
        )

    def test_negative_ramp_treated_as_step(self) -> None:
        """A negative ramp falls back to the hard step path."""
        assert (
            daytime_weight(_local_at(12, 0), day_start=DAY_START, day_end=DAY_END, ramp_minutes=-5)
            == 1.0
        )


class TestIsNighttime:
    def test_core_night(self) -> None:
        assert is_nighttime(_local_at(2, 0), day_start=DAY_START, day_end=DAY_END) is True

    def test_core_day(self) -> None:
        assert is_nighttime(_local_at(12, 0), day_start=DAY_START, day_end=DAY_END) is False

    def test_morning_ramp_not_nighttime(self) -> None:
        """Conservative: any non-zero daytime weight disqualifies nighttime."""
        assert (
            is_nighttime(
                _local_at(6, 0),
                day_start=DAY_START,
                day_end=DAY_END,
                ramp_minutes=RAMP,
            )
            is False
        )

    def test_evening_ramp_not_nighttime(self) -> None:
        assert (
            is_nighttime(
                _local_at(22, 0),
                day_start=DAY_START,
                day_end=DAY_END,
                ramp_minutes=RAMP,
            )
            is False
        )

    def test_ramp_default_zero_means_step(self) -> None:
        """Default ramp_minutes=0 with no kwarg makes is_nighttime a hard step."""
        assert is_nighttime(_local_at(22, 0), day_start=DAY_START, day_end=DAY_END) is True


class TestEffectiveThreshold:
    BAND = DayNightBand(daytime_pct=15.0, nighttime_pct=50.0)

    def test_core_day_returns_daytime_pct(self) -> None:
        threshold = effective_threshold(
            self.BAND,
            now_local=_local_at(12, 0),
            day_start=DAY_START,
            day_end=DAY_END,
            ramp_minutes=RAMP,
        )
        assert threshold == pytest.approx(15.0)

    def test_core_night_returns_nighttime_pct(self) -> None:
        threshold = effective_threshold(
            self.BAND,
            now_local=_local_at(2, 0),
            day_start=DAY_START,
            day_end=DAY_END,
            ramp_minutes=RAMP,
        )
        assert threshold == pytest.approx(50.0)

    def test_morning_boundary_is_midpoint(self) -> None:
        threshold = effective_threshold(
            self.BAND,
            now_local=_local_at(6, 0),
            day_start=DAY_START,
            day_end=DAY_END,
            ramp_minutes=RAMP,
        )
        # Midpoint of 15 and 50 = 32.5
        assert threshold == pytest.approx(32.5)

    def test_evening_boundary_is_midpoint(self) -> None:
        threshold = effective_threshold(
            self.BAND,
            now_local=_local_at(22, 0),
            day_start=DAY_START,
            day_end=DAY_END,
            ramp_minutes=RAMP,
        )
        assert threshold == pytest.approx(32.5)

    def test_quarter_through_morning_ramp(self) -> None:
        # 06:00 - 15 min + (30/4) min = 05:52:30; weight = 7.5/30 = 0.25
        # threshold = 0.25 * 15 + 0.75 * 50 = 3.75 + 37.5 = 41.25
        threshold = effective_threshold(
            self.BAND,
            now_local=_local_at(5, 52, 30),
            day_start=DAY_START,
            day_end=DAY_END,
            ramp_minutes=RAMP,
        )
        assert threshold == pytest.approx(41.25)


class TestIntegrationWithToLocal:
    """End-to-end sanity: UTC ➜ local ➜ daytime_weight uses the right hour."""

    def test_utc_noon_in_ny_is_morning_ramp(self) -> None:
        """2026-05-13 12:00 UTC = 08:00 EDT (well into daytime)."""
        utc = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
        ny = to_local(utc, tz_name="America/New_York")
        assert daytime_weight(ny, day_start=DAY_START, day_end=DAY_END, ramp_minutes=RAMP) == 1.0

    def test_utc_05_in_ny_is_night(self) -> None:
        """2026-05-13 05:00 UTC = 01:00 EDT (core night)."""
        utc = datetime(2026, 5, 13, 5, 0, 0, tzinfo=UTC)
        ny = to_local(utc, tz_name="America/New_York")
        assert daytime_weight(ny, day_start=DAY_START, day_end=DAY_END, ramp_minutes=RAMP) == 0.0

    def test_round_trip_one_day(self) -> None:
        """Sanity: incrementing 24h preserves the local-time hour."""
        utc = datetime(2026, 5, 13, 12, 0, 0, tzinfo=UTC)
        next_utc = utc + timedelta(days=1)
        assert (
            to_local(utc, tz_name="America/New_York").hour
            == to_local(next_utc, tz_name="America/New_York").hour
        )
