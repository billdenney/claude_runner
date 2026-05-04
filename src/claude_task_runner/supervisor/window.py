"""Window math for the supervisor: next reset, time until reset, EOW guard.

The Plan agent's review (ADR-0009 context) flagged clock-skew as a real
risk: comparing absolute datetimes from the host clock against the OAuth
API's reported reset times can drift apart if the host clock is off. We
mitigate by using **monotonic-elapsed** time deltas wherever possible and
treating Anthropic's reported reset times as advisory anchors.

Two flavors of "time until reset":

* :func:`time_until_reset_s` — uses the parsed reset datetime if
  available; falls back to ``last_known_reset + window_length`` when the
  reset string couldn't be parsed (graceful degrade per ADR-0008).
* :func:`crossed_reset` — detects whether a reset boundary was crossed
  between two consecutive readings. Distinguishes the legitimate
  "utilization went down because the window reset" case from the
  monotonicity drift in :mod:`usage.drift`.

Pure module. All datetime work goes through the injected :class:`Clock`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from claude_task_runner.clock import Clock
from claude_task_runner.usage.models import UsageReading, WindowReading

FIVE_HOUR_LENGTH_S: float = 5 * 3600
"""Nominal length of the 5-hour window."""

SEVEN_DAY_LENGTH_S: float = 7 * 24 * 3600
"""Nominal length of the 7-day weekly window."""


def time_until_reset_s(
    window: WindowReading,
    *,
    clock: Clock,
    fallback_window_length_s: float,
) -> float:
    """Seconds remaining until ``window`` resets.

    If ``window.resets_at`` was parsed successfully, returns the delta
    against ``clock.now()`` — clamped to ``[0, ∞)`` so a slightly-stale
    reading doesn't yield negative remaining-time.

    If ``resets_at`` is ``None`` (parse failure earlier), returns
    ``fallback_window_length_s`` as a conservative upper bound — the
    supervisor treats this as "we don't know precisely when reset is,
    but assume we have at least a full window ahead". Combined with
    ``[throttle].band_*`` bands, this keeps the supervisor in safe
    territory until the next clean reading.
    """
    if window.resets_at is None:
        return float(fallback_window_length_s)
    delta = (window.resets_at - clock.now()).total_seconds()
    return max(0.0, delta)


def crossed_reset(
    *,
    previous: WindowReading | None,
    current: WindowReading,
    clock: Clock,
    grace_s: float = 60.0,
) -> bool:
    """Did a reset boundary occur between ``previous`` and ``current``?

    A reset is recognized when:

    1. ``previous.resets_at`` is in the past (i.e., we have crossed it
       per the host clock, with ``grace_s`` slack for clock skew); OR
    2. ``current.resets_at`` is FURTHER IN THE FUTURE than
       ``previous.resets_at`` was (i.e., a new window started so the
       reset target jumped forward by ~one window).

    Returns ``False`` if either reading lacks a parsed ``resets_at`` or
    if ``previous`` is ``None`` (cold start — caller sees this as
    "no comparison available").

    Used by the supervisor to distinguish a legitimate drop in
    utilization across a reset boundary from the spurious-decrease
    monotonicity drift in :mod:`usage.drift`.
    """
    if previous is None or previous.resets_at is None or current.resets_at is None:
        return False

    now = clock.now()
    grace = timedelta(seconds=grace_s)

    # Case 1: previous reset target is now in the past (with grace).
    if previous.resets_at <= now + grace:
        return True

    # Case 2: current reset target jumped forward by approximately one
    # window length — a fresh window started.
    return current.resets_at > previous.resets_at + grace


def crossed_reset_5h(
    *,
    previous: UsageReading | None,
    current: UsageReading,
    clock: Clock,
    grace_s: float = 60.0,
) -> bool:
    """Convenience: crossed_reset on the 5-hour windows of two readings."""
    return crossed_reset(
        previous=previous.five_hour if previous else None,
        current=current.five_hour,
        clock=clock,
        grace_s=grace_s,
    )


def crossed_reset_weekly(
    *,
    previous: UsageReading | None,
    current: UsageReading,
    clock: Clock,
    grace_s: float = 60.0,
) -> bool:
    """Convenience: crossed_reset on the weekly windows of two readings."""
    return crossed_reset(
        previous=previous.seven_day if previous else None,
        current=current.seven_day,
        clock=clock,
        grace_s=grace_s,
    )


def in_eow_push_window(
    *,
    weekly: WindowReading,
    clock: Clock,
    eow_window_s: float,
) -> bool:
    """True if the weekly reset is within ``eow_window_s`` of now.

    Used by the state machine to decide whether to enter
    :class:`states.EndOfWeekPush`. Returns ``False`` if
    ``resets_at`` couldn't be parsed (we'd rather skip the push than
    push blind).
    """
    if weekly.resets_at is None:
        return False
    until = (weekly.resets_at - clock.now()).total_seconds()
    return 0.0 < until <= float(eow_window_s)


def schedule_window_start_wakeup(
    *,
    window: WindowReading,
    clock: Clock,
    delay_s: float,
    fallback_window_length_s: float,
) -> datetime:
    """Compute when the supervisor should wake up after a window reset.

    Returns ``window.resets_at + delay_s`` if reset time is parseable,
    else ``clock.now() + fallback_window_length_s + delay_s``.

    The ``delay_s`` (typically 5 minutes per ``[supervisor].window_start_delay_s``)
    gives the OAuth API a moment to register the new window before we
    start dispatching against it.
    """
    if window.resets_at is not None:
        return window.resets_at + timedelta(seconds=delay_s)
    return clock.now() + timedelta(seconds=fallback_window_length_s + delay_s)
