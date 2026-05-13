"""Dynamic weekly pacing curve: pure functions, no I/O.

The static weekly bands (``[throttle.weekly].band_*``, ``pause_at_pct``)
are a *hard* safety floor — they prevent the runner from blowing past
the weekly cap entirely. But on their own they don't tell the runner
*when* to use tokens through the week: a fast start that hits 90% on
day 2 is "fine" by the static bands and pauses for five days, which is
exactly what the operator wanted to avoid.

This module computes a *target* utilization curve anchored to the
OAuth-reported reset timestamp (NOT a fixed weekday — Anthropic's
weekly window is rolling and the reset can shift), then shifts the
effective bands up or down based on how far observed utilization
deviates from target. Outside a configurable slack region the bands
move; inside the slack region the static bands apply.

Curve shape (piecewise linear):

    target_pct
        ▲
   95 % ┤                                            ╭───
        │                                       ╭───╯
   80 % ┤                                  ╭────╯   ← EOW window
        │                            ╭─────╯
        │                       ╭────╯
        │                  ╭────╯
        │             ╭────╯
        │        ╭────╯
        │   ╭────╯
        ╰───╯───────────────────────────┼──────────────►
        0                            85 %         100 %   elapsed in week

ADR-0016 explains the rationale.
"""

from __future__ import annotations

from datetime import datetime

SEVEN_DAYS_S: float = 7 * 24 * 3600


def elapsed_fraction(
    *,
    resets_at: datetime,
    now: datetime,
    window_length_s: float = SEVEN_DAYS_S,
) -> float:
    """Fraction of the weekly window elapsed at ``now``, clamped to ``[0.0, 1.0]``.

    ``resets_at`` is the moment the *current* window closes (from
    ``UsageReading.seven_day.resets_at``). The window opened
    ``window_length_s`` before that. We compute::

        elapsed = 1 - (resets_at - now) / window_length_s

    Any negative result (now past reset) clamps to 1.0; any result > 1.0
    (clock far behind reset, e.g. drift) clamps to 1.0 too. A result < 0.0
    (now before window opened, exotic edge) clamps to 0.0.

    Raises :class:`ValueError` if ``window_length_s <= 0``.
    """
    if window_length_s <= 0:
        raise ValueError("window_length_s must be positive")
    remaining_s = (resets_at - now).total_seconds()
    raw = 1.0 - remaining_s / window_length_s
    return max(0.0, min(1.0, raw))


def target_weekly_pct(
    elapsed: float,
    *,
    eow_target_pct: float,
    eow_window_fraction: float,
    pre_eow_target_pct: float,
) -> float:
    """Target utilization (%) at a given elapsed fraction of the weekly window.

    Piecewise linear:

    * ``0 ≤ elapsed ≤ 1 - eow_window_fraction``: ramp from 0 to
      ``pre_eow_target_pct``.
    * ``1 - eow_window_fraction < elapsed ≤ 1``: ramp from
      ``pre_eow_target_pct`` to ``eow_target_pct``.

    Defensive clamps:

    * ``elapsed`` is clamped to ``[0, 1]``.
    * ``eow_window_fraction`` is clamped to ``[0, 1]``; ``0`` collapses
      the EOW segment (entire week is the pre-EOW ramp, ``eow_target_pct``
      unused); ``1`` collapses the pre-EOW segment (entire week is EOW,
      ramps to ``eow_target_pct``).

    The output is clamped to ``[0, 100]``.
    """
    elapsed = max(0.0, min(1.0, elapsed))
    eow_frac = max(0.0, min(1.0, eow_window_fraction))
    breakpoint = 1.0 - eow_frac

    if eow_frac == 0.0:
        # No EOW segment: linear to pre_eow_target_pct across the whole window.
        target = elapsed * pre_eow_target_pct
    elif eow_frac == 1.0:
        # Whole window is EOW: linear to eow_target_pct.
        target = elapsed * eow_target_pct
    elif elapsed <= breakpoint:
        target = (elapsed / breakpoint) * pre_eow_target_pct
    else:
        frac_in_eow = (elapsed - breakpoint) / eow_frac
        target = pre_eow_target_pct + frac_in_eow * (eow_target_pct - pre_eow_target_pct)

    return max(0.0, min(100.0, target))


def adjusted_weekly_band(
    *,
    observed_pct: float,
    target_now: float,
    base_pct: int,
    slack_pp: float,
    min_pct: int = 0,
    max_pct: int = 100,
) -> int:
    """Shift a static weekly band by the observed-vs-target deviation.

    * Within the ``slack_pp`` dead-band of ``target_now``: no change,
      returns ``base_pct``.
    * Ahead of target by more than ``slack_pp``: tighten (lower threshold)
      by ``(observed - target) - slack_pp`` percentage points.
    * Behind target by more than ``slack_pp``: loosen (raise threshold)
      by ``(target - observed) - slack_pp`` percentage points.

    Result is clamped to ``[min_pct, max_pct]`` and returned as an int
    (rounded half-up). The static band schema is integer-typed so we
    quantize to match.
    """
    deviation = observed_pct - target_now
    if abs(deviation) <= slack_pp:
        return max(min_pct, min(max_pct, base_pct))

    if deviation > 0:
        # Ahead of target: tighter band.
        shift = deviation - slack_pp
        adjusted = base_pct - shift
    else:
        # Behind target: looser band.
        shift = -deviation - slack_pp
        adjusted = base_pct + shift

    return max(min_pct, min(max_pct, round(adjusted)))
