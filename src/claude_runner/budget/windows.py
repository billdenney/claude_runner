"""Token-usage accounting windows (5-hour rolling and 7-day weekly)."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

Clock = object  # callable returning datetime; typed loosely to allow lambdas


def _default_clock() -> datetime:
    return datetime.now(tz=UTC)


@dataclass(slots=True)
class _Event:
    at: datetime
    tokens: int


@dataclass
class RollingWindow:
    """Rolling window over `duration_s` seconds."""

    duration: timedelta = timedelta(hours=5)
    _events: deque[_Event] = field(default_factory=deque)

    def record(self, tokens: int, at: datetime | None = None) -> None:
        if tokens <= 0:
            return
        now = at or _default_clock()
        self._events.append(_Event(now, tokens))
        self._evict(now)

    def used(self, at: datetime | None = None) -> int:
        now = at or _default_clock()
        self._evict(now)
        return sum(e.tokens for e in self._events)

    def oldest_event_at(self) -> datetime | None:
        return self._events[0].at if self._events else None

    def next_reset(self, at: datetime | None = None) -> datetime:
        """Earliest time the window has meaningful capacity back.

        Defined as the moment the oldest in-window event ages out; if empty,
        returns `at` (no waiting needed).
        """
        now = at or _default_clock()
        if not self._events:
            return now
        return self._events[0].at + self.duration

    def _evict(self, now: datetime) -> None:
        cutoff = now - self.duration
        while self._events and self._events[0].at < cutoff:
            self._events.popleft()


@dataclass
class WeeklyWindow:
    """Fixed weekly window anchored at `anchor_weekday` (Monday=0) 00:00 local."""

    anchor_weekday: int = 0  # Monday
    _events: list[_Event] = field(default_factory=list)

    def record(self, tokens: int, at: datetime | None = None) -> None:
        if tokens <= 0:
            return
        now = at or _default_clock()
        anchor = self._anchor_for(now)
        # Drop events from before the current week's anchor.
        self._events = [e for e in self._events if e.at >= anchor]
        self._events.append(_Event(now, tokens))

    def used(self, at: datetime | None = None) -> int:
        now = at or _default_clock()
        anchor = self._anchor_for(now)
        return sum(e.tokens for e in self._events if e.at >= anchor)

    def next_reset(self, at: datetime | None = None) -> datetime:
        now = at or _default_clock()
        return self._anchor_for(now) + timedelta(days=7)

    def days_remaining(self, at: datetime | None = None) -> float:
        now = at or _default_clock()
        return (self.next_reset(now) - now).total_seconds() / 86_400.0

    def in_last_day(self, at: datetime | None = None) -> bool:
        return self.days_remaining(at) <= 1.0

    def _anchor_for(self, now: datetime) -> datetime:
        # Use the date of `now` in its tz; anchor at 00:00 on the anchor weekday.
        day_offset = (now.weekday() - self.anchor_weekday) % 7
        anchor_date = (now - timedelta(days=day_offset)).date()
        tz = now.tzinfo or UTC
        return datetime.combine(anchor_date, datetime.min.time(), tzinfo=tz)
