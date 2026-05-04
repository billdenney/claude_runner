"""Parse the 'Resets ...' strings emitted by `claude /usage`.

Two known formats observed in the TUI:

  5-hour window:  ``2:10am (UTC)``                 (clock time only; no date)
  7-day window:   ``May 4, 3am (UTC)``             (month + day + clock; no year)

These functions are intentionally non-fatal: a string that can't be parsed
returns ``None`` instead of raising. The supervisor uses the last-known
reset + window length as a fallback so a reset-time format change doesn't
kill dispatching. See ADR-0008.

We rely only on `(UTC)` markers in the strings; non-UTC strings return None.
The `clock` argument is injected per ADR-0009 so tests are deterministic
across midnight / year boundaries.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, time, timedelta

from claude_task_runner.clock import Clock

_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

# 5-hour: "2:10am (UTC)", "12am (UTC)", "11:59pm (UTC)"
_FIVE_HOUR_RE = re.compile(
    r"""
    ^\s*
    (?P<hour>\d{1,2})
    (?::(?P<minute>\d{2}))?
    \s*(?P<meridiem>am|pm)
    \s*\((?P<tz>UTC)\)
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

# 7-day: "May 4, 3am (UTC)", "May 4, 3:15am (UTC)"
_WEEKLY_RE = re.compile(
    r"""
    ^\s*
    (?P<month>[A-Za-z]+)
    \s+
    (?P<day>\d{1,2})
    ,\s*
    (?P<hour>\d{1,2})
    (?::(?P<minute>\d{2}))?
    \s*(?P<meridiem>am|pm)
    \s*\((?P<tz>UTC)\)
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _to_24h(hour: int, meridiem: str) -> int | None:
    """Convert 12h clock to 24h. Returns None on out-of-range."""
    m = meridiem.lower()
    if hour < 1 or hour > 12:
        return None
    if m == "am":
        return 0 if hour == 12 else hour
    if m == "pm":
        return 12 if hour == 12 else hour + 12
    return None


def parse_five_hour(text: str, clock: Clock) -> datetime | None:
    """Parse a 5-hour-window reset string.

    The TUI emits only a clock time (no date), so we attach the current
    UTC date and roll forward by one day if the parsed time has already
    passed today.
    """
    match = _FIVE_HOUR_RE.match(text or "")
    if match is None:
        return None
    try:
        hour = int(match.group("hour"))
        minute = int(match.group("minute") or 0)
        if not 0 <= minute <= 59:
            return None
    except ValueError:
        return None
    hour24 = _to_24h(hour, match.group("meridiem"))
    if hour24 is None:
        return None

    now = clock.now()
    candidate = datetime.combine(now.date(), time(hour24, minute, tzinfo=UTC))
    if candidate <= now:
        candidate = candidate + timedelta(days=1)
    return candidate


def parse_weekly(text: str, clock: Clock) -> datetime | None:
    """Parse a 7-day-window reset string.

    The TUI emits month + day + clock time (no year). We attach the
    current UTC year, then roll forward to next year if the parsed date
    has already passed.
    """
    match = _WEEKLY_RE.match(text or "")
    if match is None:
        return None
    try:
        day = int(match.group("day"))
        hour = int(match.group("hour"))
        minute = int(match.group("minute") or 0)
        if not 0 <= minute <= 59:
            return None
    except ValueError:
        return None
    month = _MONTHS.get(match.group("month").lower())
    if month is None:
        return None
    hour24 = _to_24h(hour, match.group("meridiem"))
    if hour24 is None:
        return None

    now = clock.now()
    try:
        candidate = datetime(now.year, month, day, hour24, minute, tzinfo=UTC)
    except ValueError:
        # e.g. February 30 — invalid date.
        return None
    if candidate <= now:
        try:
            candidate = candidate.replace(year=now.year + 1)
        except ValueError:
            # Feb 29 of a non-leap next year. Bump to Feb 28.
            candidate = candidate.replace(year=now.year + 1, day=28)
    return candidate
