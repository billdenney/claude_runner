"""Tests for the Clock protocol implementations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from claude_task_runner.clock import FakeClock, RealClock


class TestRealClock:
    def test_now_returns_utc(self) -> None:
        clock = RealClock()
        now = clock.now()
        assert now.tzinfo is not None

    def test_monotonic_advances(self) -> None:
        clock = RealClock()
        a = clock.monotonic()
        b = clock.monotonic()
        assert b >= a


class TestFakeClock:
    def test_initial_now(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        clock = FakeClock(start)
        assert clock.now() == start
        assert clock.monotonic() == 0.0

    def test_advance_moves_forward(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        clock = FakeClock(start)
        clock.advance(60)
        assert clock.now() == start + timedelta(seconds=60)
        assert clock.monotonic() == 60.0

    def test_advance_negative_rejected(self) -> None:
        clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
        with pytest.raises(ValueError, match="cannot move backward"):
            clock.advance(-1)

    def test_naive_start_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            FakeClock(datetime(2026, 1, 1))

    def test_set_to_advances(self) -> None:
        start = datetime(2026, 1, 1, tzinfo=UTC)
        clock = FakeClock(start)
        target = start + timedelta(hours=3)
        clock.set_to(target)
        assert clock.now() == target
        assert clock.monotonic() == 3 * 3600

    def test_set_to_backward_rejected(self) -> None:
        clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
        with pytest.raises(ValueError, match="cannot move backward"):
            clock.set_to(datetime(2025, 12, 31, tzinfo=UTC))

    def test_set_to_naive_rejected(self) -> None:
        clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
        with pytest.raises(ValueError, match="timezone-aware"):
            clock.set_to(datetime(2026, 1, 2))

    def test_normalizes_non_utc_start_to_utc(self) -> None:
        from datetime import timezone

        eastern = timezone(timedelta(hours=-5))
        start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=eastern)
        clock = FakeClock(start)
        # 12:00 EST == 17:00 UTC
        assert clock.now() == datetime(2026, 1, 1, 17, 0, 0, tzinfo=UTC)
