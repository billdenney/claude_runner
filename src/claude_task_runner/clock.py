"""Clock protocol injected throughout the codebase for testability.

Direct calls to `datetime.now()`, `datetime.utcnow()`, or `time.monotonic()`
outside of this module are flagged in code review. See ADR-0009.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    """Time source. Implementations must return UTC datetimes."""

    def now(self) -> datetime: ...
    def monotonic(self) -> float: ...


class RealClock:
    """Production clock backed by `datetime.now(UTC)` and `time.monotonic()`."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


class FakeClock:
    """Test clock that advances on demand. Always returns UTC datetimes."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("FakeClock start must be timezone-aware")
        self._now = start.astimezone(UTC)
        self._mono = 0.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("Clock cannot move backward")
        self._now = self._now + timedelta(seconds=seconds)
        self._mono += seconds

    def set_to(self, when: datetime) -> None:
        if when.tzinfo is None:
            raise ValueError("set_to requires a timezone-aware datetime")
        target = when.astimezone(UTC)
        if target < self._now:
            raise ValueError("Clock cannot move backward")
        delta = (target - self._now).total_seconds()
        self._now = target
        self._mono += delta
