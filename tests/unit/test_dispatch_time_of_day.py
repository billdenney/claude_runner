"""Tests for the wrap-aware day/night band selector (ADR-0022)."""

from __future__ import annotations

from datetime import UTC, datetime, time

import pytest

from claude_task_runner.throttle.time_of_day import parse_hhmm, to_local, which_band


class TestParseHHMM:
    def test_valid_morning(self) -> None:
        assert parse_hhmm("06:00") == time(6, 0)

    def test_valid_evening(self) -> None:
        assert parse_hhmm("21:30") == time(21, 30)

    def test_midnight(self) -> None:
        assert parse_hhmm("00:00") == time(0, 0)

    def test_last_minute(self) -> None:
        assert parse_hhmm("23:59") == time(23, 59)

    def test_rejects_25_hour(self) -> None:
        with pytest.raises(ValueError, match="HH:MM"):
            parse_hhmm("25:00")

    def test_rejects_60_minute(self) -> None:
        with pytest.raises(ValueError, match="HH:MM"):
            parse_hhmm("12:60")

    def test_rejects_no_zero_pad(self) -> None:
        with pytest.raises(ValueError, match="HH:MM"):
            parse_hhmm("6:00")

    def test_rejects_seconds(self) -> None:
        with pytest.raises(ValueError, match="HH:MM"):
            parse_hhmm("06:00:00")

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="HH:MM"):
            parse_hhmm("")


class TestToLocal:
    def test_naive_input_rejected(self) -> None:
        naive = datetime(2026, 5, 27, 12, 0)
        with pytest.raises(ValueError, match="timezone-aware"):
            to_local(naive)

    def test_empty_tz_uses_system_local(self) -> None:
        # Just confirms it doesn't raise and the result is tz-aware.
        utc = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
        result = to_local(utc, "")
        assert result.tzinfo is not None

    def test_explicit_iana(self) -> None:
        utc = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)
        result = to_local(utc, "America/New_York")
        # 12:00 UTC on 2026-05-27 is 08:00 EDT (UTC-4).
        assert result.hour == 8
        assert result.tzinfo is not None


class TestWhichBandWrapMidnight:
    """night_start > night_end: night wraps midnight (e.g. 21:00 → 06:00)."""

    NS = time(21, 0)
    NE = time(6, 0)

    def _band_at(self, hh: int, mm: int = 0) -> str:
        now = datetime(2026, 5, 27, hh, mm, tzinfo=UTC)
        return which_band(now, night_start=self.NS, night_end=self.NE)

    def test_before_night_starts(self) -> None:
        assert self._band_at(20, 59) == "day"

    def test_at_night_start(self) -> None:
        assert self._band_at(21, 0) == "night"

    def test_after_night_start(self) -> None:
        assert self._band_at(23, 0) == "night"

    def test_at_midnight(self) -> None:
        assert self._band_at(0, 0) == "night"

    def test_predawn(self) -> None:
        assert self._band_at(5, 0) == "night"

    def test_at_night_end(self) -> None:
        # End is exclusive: 06:00 is the first day minute.
        assert self._band_at(6, 0) == "day"

    def test_morning(self) -> None:
        assert self._band_at(12, 0) == "day"


class TestWhichBandNoWrap:
    """night_start < night_end: night is the same-day window (e.g. 01:00 → 10:00)."""

    NS = time(1, 0)
    NE = time(10, 0)

    def _band_at(self, hh: int, mm: int = 0) -> str:
        now = datetime(2026, 5, 27, hh, mm, tzinfo=UTC)
        return which_band(now, night_start=self.NS, night_end=self.NE)

    def test_pre_midnight(self) -> None:
        assert self._band_at(0, 30) == "day"

    def test_at_night_start(self) -> None:
        assert self._band_at(1, 0) == "night"

    def test_inside_night(self) -> None:
        assert self._band_at(5, 0) == "night"

    def test_at_night_end_exclusive(self) -> None:
        assert self._band_at(10, 0) == "day"

    def test_late_morning(self) -> None:
        assert self._band_at(12, 0) == "day"

    def test_evening(self) -> None:
        assert self._band_at(23, 0) == "day"


class TestWhichBandDegenerate:
    def test_zero_length_night_is_always_day(self) -> None:
        ns = ne = time(12, 0)
        for hh in range(24):
            now = datetime(2026, 5, 27, hh, 0, tzinfo=UTC)
            assert which_band(now, night_start=ns, night_end=ne) == "day"


class TestWhichBandExclusivity:
    """Property: exactly one of {day, night} for every minute, never both, never neither."""

    @pytest.mark.parametrize(
        ("night_start", "night_end"),
        [
            (time(21, 0), time(6, 0)),  # wrap
            (time(1, 0), time(10, 0)),  # no-wrap
            (time(0, 0), time(8, 30)),  # boundary at midnight
            (time(20, 0), time(0, 0)),  # boundary at midnight (wrap, end at 00:00)
        ],
    )
    def test_total_and_exclusive(self, night_start: time, night_end: time) -> None:
        for hh in range(24):
            for mm in (0, 15, 30, 45, 59):
                now = datetime(2026, 5, 27, hh, mm, tzinfo=UTC)
                band = which_band(now, night_start=night_start, night_end=night_end)
                assert band in {"day", "night"}, f"{hh}:{mm} → {band!r}"
