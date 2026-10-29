"""Tests for the Clock protocol implementations."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

import pytest

from claude_task_runner.clock import (
    FakeClock,
    RealClock,
    TimestampOutOfRange,
    utc_from_timestamp,
)


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


# ---------------------------------------------------------------------------
# utc_from_timestamp: a POSIX timestamp to an aware UTC datetime
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        pytest.param(0, datetime(1970, 1, 1, tzinfo=UTC), id="epoch"),
        pytest.param(1.5, datetime(1970, 1, 1, 0, 0, 1, 500_000, tzinfo=UTC), id="fraction"),
        pytest.param(
            253402300799, datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC), id="last-second"
        ),
    ],
)
def test_utc_from_timestamp_known_answers(timestamp: float, expected: datetime) -> None:
    got = utc_from_timestamp(timestamp)
    assert got == expected
    assert got.tzinfo is UTC


@pytest.mark.parametrize(
    ("timestamp", "cause"),
    [
        pytest.param(253402300800, ValueError, id="year-10000"),
        pytest.param(-62135596801, ValueError, id="year-0"),
        pytest.param(float("nan"), ValueError, id="nan"),
        pytest.param(float("inf"), OverflowError, id="inf"),
        pytest.param(1e20, OverflowError, id="past-time_t"),
        pytest.param(
            2**63 - 1,
            OSError,
            id="gmtime-errno-75",
            marks=pytest.mark.skipif(
                not sys.platform.startswith("linux"), reason="glibc gmtime's EOVERFLOW"
            ),
        ),
    ],
)
def test_utc_from_timestamp_fails_one_typed_way(timestamp: float, cause: type) -> None:
    """Each way ``fromtimestamp`` fails becomes TimestampOutOfRange, a
    ValueError, chained to the original, so one ``except`` covers them."""
    with pytest.raises(TimestampOutOfRange) as exc_info:
        utc_from_timestamp(timestamp)
    assert isinstance(exc_info.value, ValueError)
    assert type(exc_info.value.__cause__) is cause
    assert str(exc_info.value).startswith(f"timestamp {timestamp!r} is out of range: ")
