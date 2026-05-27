"""Wrap-aware day/night band selection for the 5h side (ADR-0022).

The dispatch-pct schema's ``[dispatch_pct.night]`` declares a window via
``time_start`` / ``time_end``; the day band is the implicit complement.
Boundaries are a **hard step** — no ramp_minutes interpolation (the
existing 30-minute ramp was ~30 ticks at a 60s poll cadence and never
made an observable difference). The night window may wrap midnight
(``time_start > time_end``, e.g. ``21:00 → 06:00``) or not
(``time_start < time_end``, e.g. ``01:00 → 10:00``).

Pure module: no I/O, no settings reading.
"""

from __future__ import annotations

import re
import zoneinfo
from datetime import datetime, time
from typing import Final

_HHMM_RE: Final = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def parse_hhmm(value: str) -> time:
    """Parse a 24-hour ``HH:MM`` string into a :class:`datetime.time`.

    Raises :class:`ValueError` on malformed input.
    """
    if not _HHMM_RE.match(value):
        raise ValueError(f"expected 'HH:MM' 24-hour time, got {value!r}")
    hh_str, mm_str = value.split(":")
    return time(hour=int(hh_str), minute=int(mm_str))


def to_local(now_utc: datetime, tz_name: str = "") -> datetime:
    """Convert a UTC datetime to local time per ``tz_name``.

    Empty ``tz_name`` defers to the system local timezone (via
    :meth:`datetime.astimezone` with no argument). A non-empty value
    must be a valid IANA name. The input must be timezone-aware —
    naive datetimes raise :class:`ValueError`.
    """
    if now_utc.tzinfo is None:
        raise ValueError("to_local() requires a timezone-aware datetime")
    if not tz_name:
        return now_utc.astimezone()
    return now_utc.astimezone(zoneinfo.ZoneInfo(tz_name))


def which_band(
    now_local: datetime,
    *,
    night_start: time,
    night_end: time,
) -> str:
    """Return ``"day"`` or ``"night"`` for ``now_local``.

    Window semantics:

    * ``night_start < night_end``: night is ``[start, end)`` the same
      day (e.g. ``01:00 → 10:00``).
    * ``night_start > night_end``: night wraps midnight —
      ``[start, 24:00)`` union ``[00:00, end)`` (e.g. ``21:00 → 06:00``).
    * ``night_start == night_end``: zero-length night → always day
      (degenerate but accepted).

    The boundary is a hard step: at ``night_start`` exactly the band
    flips from day to night; at ``night_end`` exactly the band flips
    back to day.

    Seconds within the minute are intentionally ignored — the band
    decision changes at most twice per day, and the poll cadence
    (~60s) is the natural granularity.
    """
    minute = now_local.hour * 60 + now_local.minute
    start = night_start.hour * 60 + night_start.minute
    end = night_end.hour * 60 + night_end.minute

    if start == end:
        return "day"
    if start < end:
        return "night" if start <= minute < end else "day"
    return "night" if (minute >= start or minute < end) else "day"


__all__ = ["parse_hhmm", "to_local", "which_band"]
