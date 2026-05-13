"""Time-of-day band modulation: pure functions, no I/O.

The supervisor's throttle bands shrink during operator-active hours and
loosen overnight so a long-running queue doesn't compete with interactive
work. This module computes the *effective* threshold for the current
moment given a daytime value, a nighttime value, the day-window
boundaries, and a smooth ramp width.

Design notes:

* Ramps are **centered** on the configured ``day_start`` / ``day_end``
  boundaries. With ``day_start = 06:00`` and ``ramp_minutes = 30``, the
  morning transition runs from 05:45 → 06:15 — half the ramp before the
  boundary, half after. At the boundary itself the effective threshold
  is exactly the midpoint of ``daytime_pct`` and ``nighttime_pct``.

* Day windows are assumed *not* to wrap midnight (i.e. ``day_start <
  day_end``). Overnight-active operators can express their schedule by
  setting ``day_start`` to the wake hour and ``day_end`` to the bed
  hour anyway, treating "day" as their core active block; only the
  semantics of the field names get awkward, not the math.

* ``is_nighttime`` is conservative: only ``True`` in **core** night
  (outside both ramps). The EOW-push state in the supervisor uses this
  to avoid kicking off a push that immediately runs into the morning
  ramp.

See ADR-0015 for the rationale and the token-math anchor that motivated
the default daytime/nighttime band values.
"""

from __future__ import annotations

import zoneinfo
from dataclasses import dataclass
from datetime import datetime, time


@dataclass(frozen=True)
class DayNightBand:
    """A throttle band threshold split by time of day.

    Both fields are percentage points in ``[0, 100]``.
    """

    daytime_pct: float
    nighttime_pct: float


def parse_hhmm(value: str) -> time:
    """Parse a ``HH:MM`` 24-hour string into a :class:`datetime.time`.

    Raises :class:`ValueError` on malformed input — the supervisor lets
    this propagate to the config validator so misconfiguration fails
    loudly at startup rather than silently picking a wrong boundary.
    """
    hh_str, _, mm_str = value.partition(":")
    if not hh_str or not mm_str:
        raise ValueError(f"expected HH:MM, got {value!r}")
    hh = int(hh_str)
    mm = int(mm_str)
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"HH:MM out of range: {value!r}")
    return time(hour=hh, minute=mm)


def to_local(now_utc: datetime, tz_name: str = "") -> datetime:
    """Convert a UTC datetime to local time per ``tz_name``.

    Empty ``tz_name`` defers to the system local timezone (via
    :meth:`datetime.astimezone` with no argument). A non-empty value
    must be a valid IANA name (e.g. ``"America/New_York"``); invalid
    names raise :class:`zoneinfo.ZoneInfoNotFoundError`.

    The input MUST be timezone-aware; a naive datetime raises
    :class:`ValueError` because ambiguous-local-time math is a
    foot-gun we'd rather not paper over.
    """
    if now_utc.tzinfo is None:
        raise ValueError("to_local() requires a timezone-aware datetime")
    if not tz_name:
        return now_utc.astimezone()
    return now_utc.astimezone(zoneinfo.ZoneInfo(tz_name))


def _minute_of_day(t: time) -> int:
    """Minutes since 00:00."""
    return t.hour * 60 + t.minute


def daytime_weight(
    now_local: datetime,
    *,
    day_start: time,
    day_end: time,
    ramp_minutes: int,
) -> float:
    """Fraction of "daytime-ness" at ``now_local`` in ``[0.0, 1.0]``.

    ``1.0`` = squarely in core daytime, ``0.0`` = squarely in core
    nighttime, intermediate values during a ramp.

    Pure math: hour-minute-second values are read off the input; tzinfo
    is ignored (caller is responsible for converting UTC → local first
    via :func:`to_local`). Day windows are assumed not to wrap midnight,
    i.e. ``day_start < day_end``. A zero-length window (``day_start ==
    day_end``) is treated as always-nighttime.
    """
    now_min = now_local.hour * 60 + now_local.minute + now_local.second / 60
    start_min = _minute_of_day(day_start)
    end_min = _minute_of_day(day_end)

    if start_min == end_min:
        # Zero-length day window: treat as always nighttime.
        return 0.0

    if ramp_minutes <= 0:
        # Hard step: in [day_start, day_end) = day, otherwise night.
        return 1.0 if start_min <= now_min < end_min else 0.0

    half = ramp_minutes / 2.0

    # Morning ramp: [start_min - half, start_min + half]
    if start_min - half <= now_min < start_min + half:
        return max(0.0, min(1.0, (now_min - start_min + half) / ramp_minutes))

    # Evening ramp: [end_min - half, end_min + half]
    if end_min - half <= now_min < end_min + half:
        return max(0.0, min(1.0, 1.0 - (now_min - end_min + half) / ramp_minutes))

    # Outside both ramps: core day if inside [start_min, end_min), else core night.
    return 1.0 if start_min <= now_min < end_min else 0.0


def is_nighttime(
    now_local: datetime,
    *,
    day_start: time,
    day_end: time,
    ramp_minutes: int = 0,
) -> bool:
    """True iff ``now_local`` is in core nighttime (excluding ramp regions).

    Conservative by design: a moment inside the morning or evening ramp
    counts as *not* nighttime, so EOW-push state transitions don't fire
    while we're about to slide into daytime within the next ``ramp_minutes / 2``.
    """
    return (
        daytime_weight(
            now_local,
            day_start=day_start,
            day_end=day_end,
            ramp_minutes=ramp_minutes,
        )
        <= 0.0
    )


def effective_threshold(
    band: DayNightBand,
    *,
    now_local: datetime,
    day_start: time,
    day_end: time,
    ramp_minutes: int,
) -> float:
    """Interpolate the band threshold for ``now_local``.

    Returns ``band.daytime_pct`` in core day, ``band.nighttime_pct`` in
    core night, and a linear blend in between::

        threshold = w * daytime_pct + (1 - w) * nighttime_pct

    where ``w`` is the :func:`daytime_weight` at ``now_local``.
    """
    weight = daytime_weight(
        now_local,
        day_start=day_start,
        day_end=day_end,
        ramp_minutes=ramp_minutes,
    )
    return weight * band.daytime_pct + (1.0 - weight) * band.nighttime_pct
