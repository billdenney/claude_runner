"""Tests for usage drift detection (monotonicity)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from claude_task_runner.usage.drift import (
    UsageMonotonicityDrift,
    validate_monotonicity,
)
from claude_task_runner.usage.models import UsageReading, WindowReading


def make_reading(
    *,
    five_pct: int,
    week_pct: int,
    captured_at: datetime,
) -> UsageReading:
    return UsageReading(
        captured_at=captured_at,
        five_hour=WindowReading(
            utilization_pct=five_pct,
            resets_at_raw="2:10am (UTC)",
            resets_at=None,
        ),
        seven_day=WindowReading(
            utilization_pct=week_pct,
            resets_at_raw="May 4, 3am (UTC)",
            resets_at=None,
        ),
    )


class TestMonotonicity:
    def test_equal_readings_pass(self) -> None:
        t = datetime(2026, 5, 3, 18, 0, tzinfo=UTC)
        prev = make_reading(five_pct=40, week_pct=20, captured_at=t)
        curr = make_reading(five_pct=40, week_pct=20, captured_at=t + timedelta(seconds=60))
        validate_monotonicity(prev, curr, suspicious_delta_pct=50)

    def test_strict_increase_passes(self) -> None:
        t = datetime(2026, 5, 3, 18, 0, tzinfo=UTC)
        prev = make_reading(five_pct=40, week_pct=20, captured_at=t)
        curr = make_reading(five_pct=42, week_pct=21, captured_at=t + timedelta(seconds=60))
        validate_monotonicity(prev, curr, suspicious_delta_pct=50)

    def test_5h_decrease_raises(self) -> None:
        t = datetime(2026, 5, 3, 18, 0, tzinfo=UTC)
        prev = make_reading(five_pct=50, week_pct=20, captured_at=t)
        curr = make_reading(five_pct=40, week_pct=20, captured_at=t + timedelta(seconds=60))
        with pytest.raises(UsageMonotonicityDrift, match="5h utilization decreased"):
            validate_monotonicity(prev, curr, suspicious_delta_pct=50)

    def test_weekly_decrease_raises(self) -> None:
        t = datetime(2026, 5, 3, 18, 0, tzinfo=UTC)
        prev = make_reading(five_pct=20, week_pct=50, captured_at=t)
        curr = make_reading(five_pct=20, week_pct=40, captured_at=t + timedelta(seconds=60))
        with pytest.raises(UsageMonotonicityDrift, match="weekly utilization decreased"):
            validate_monotonicity(prev, curr, suspicious_delta_pct=50)

    def test_backward_capture_raises(self) -> None:
        t = datetime(2026, 5, 3, 18, 0, tzinfo=UTC)
        prev = make_reading(five_pct=40, week_pct=20, captured_at=t)
        curr = make_reading(five_pct=40, week_pct=20, captured_at=t - timedelta(seconds=60))
        with pytest.raises(UsageMonotonicityDrift, match="older than"):
            validate_monotonicity(prev, curr, suspicious_delta_pct=50)

    def test_large_jump_below_threshold_passes(self) -> None:
        t = datetime(2026, 5, 3, 18, 0, tzinfo=UTC)
        prev = make_reading(five_pct=40, week_pct=20, captured_at=t)
        curr = make_reading(five_pct=85, week_pct=20, captured_at=t + timedelta(seconds=60))
        # 45-point jump < 50-point threshold; should not raise.
        validate_monotonicity(prev, curr, suspicious_delta_pct=50)

    def test_large_jump_above_threshold_records(self) -> None:
        t = datetime(2026, 5, 3, 18, 0, tzinfo=UTC)
        prev = make_reading(five_pct=20, week_pct=10, captured_at=t)
        curr = make_reading(five_pct=80, week_pct=15, captured_at=t + timedelta(seconds=60))
        # 60-point 5h jump > 50-point threshold; recorded but does not raise.
        validate_monotonicity(prev, curr, suspicious_delta_pct=50)
        assert hasattr(validate_monotonicity, "_last_suspicious_jump")
        marker = validate_monotonicity._last_suspicious_jump  # type: ignore[attr-defined]
        assert "5h+60" in marker
