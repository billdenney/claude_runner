"""Tests for :func:`claude_task_runner.config.duration.parse_duration`."""

from __future__ import annotations

import pytest

from claude_task_runner.config.duration import DurationParseError, parse_duration


class TestSingleUnit:
    def test_hours(self) -> None:
        assert parse_duration("40h") == 40 * 3600

    def test_days(self) -> None:
        assert parse_duration("2d") == 2 * 86400

    def test_minutes(self) -> None:
        assert parse_duration("30m") == 30 * 60

    def test_seconds(self) -> None:
        assert parse_duration("45s") == 45

    def test_zero(self) -> None:
        assert parse_duration("0h") == 0.0

    def test_returns_float(self) -> None:
        assert isinstance(parse_duration("1h"), float)


class TestCombined:
    def test_day_plus_hour(self) -> None:
        # ADR-0022 example: 40h is equivalent to 1d 16h.
        assert parse_duration("1d 16h") == 40 * 3600
        assert parse_duration("1d 16h") == parse_duration("40h")

    def test_all_four_units(self) -> None:
        assert parse_duration("1d 2h 3m 4s") == 86400 + 2 * 3600 + 3 * 60 + 4

    def test_no_whitespace_between_tokens(self) -> None:
        assert parse_duration("1d16h") == 40 * 3600

    def test_leading_and_trailing_whitespace_ok(self) -> None:
        assert parse_duration("  1h  ") == 3600

    def test_multiple_spaces_between_tokens(self) -> None:
        assert parse_duration("1d   2h") == 86400 + 2 * 3600


class TestInvalid:
    def test_empty_string(self) -> None:
        with pytest.raises(DurationParseError, match="empty"):
            parse_duration("")

    def test_whitespace_only(self) -> None:
        with pytest.raises(DurationParseError, match="empty"):
            parse_duration("   ")

    def test_bare_number(self) -> None:
        with pytest.raises(DurationParseError, match="invalid duration"):
            parse_duration("40")

    def test_unit_alone(self) -> None:
        with pytest.raises(DurationParseError, match="invalid duration"):
            parse_duration("h")

    def test_unknown_unit_week(self) -> None:
        with pytest.raises(DurationParseError, match="invalid duration"):
            parse_duration("1w")

    def test_unknown_unit_letter(self) -> None:
        with pytest.raises(DurationParseError, match="invalid duration"):
            parse_duration("1x")

    def test_fractional(self) -> None:
        with pytest.raises(DurationParseError, match="invalid duration"):
            parse_duration("1.5h")

    def test_negative(self) -> None:
        with pytest.raises(DurationParseError, match="invalid duration"):
            parse_duration("-1h")

    def test_duplicate_unit(self) -> None:
        with pytest.raises(DurationParseError, match="'h' appears more than once"):
            parse_duration("1h 2h")

    def test_duplicate_unit_no_whitespace(self) -> None:
        with pytest.raises(DurationParseError, match="'h' appears more than once"):
            parse_duration("1h2h")

    def test_non_string_input(self) -> None:
        with pytest.raises(DurationParseError, match="expected str"):
            parse_duration(40)  # type: ignore[arg-type]


class TestADRExamples:
    """Examples from ADR-0022 — verify they all parse to the documented values."""

    def test_eow_time_switch_default(self) -> None:
        # `[dispatch_pct.week].eow_time_switch = "40h"` → 144_000 s.
        assert parse_duration("40h") == 144_000

    def test_eow_time_switch_alt_spelling(self) -> None:
        # An operator typing "1d 16h" should produce the same value.
        assert parse_duration("1d 16h") == 144_000
