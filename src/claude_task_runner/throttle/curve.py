"""Piecewise-linear weekly target curve and its analytical inverse.

The supervisor's weekly trace-following decision rule (ADR-0022) is:

* `target_pct(elapsed)` rises from 0 to ``early_pct`` over the pre-EOW
  segment, then from ``early_pct`` to ``eow_pct`` over the EOW segment
  (the last ``eow_window_fraction`` of the week).
* If observed > target, the supervisor stops weekly dispatch and wakes
  when the target catches up — solved analytically via
  :func:`elapsed_for_target_pct`.

The math is identical to the curve in the superseded
:mod:`supervisor.pacing`; this module renames the parameters and adds
the inverse. No I/O, no datetime work — caller passes a fraction.
"""

from __future__ import annotations

from datetime import datetime

SEVEN_DAYS_S: float = 7.0 * 24.0 * 3600.0
"""Nominal length of the 7-day weekly window."""


def elapsed_fraction(
    now: datetime,
    resets_at: datetime,
    *,
    window_s: float = SEVEN_DAYS_S,
) -> float:
    """Fraction of the weekly window elapsed at ``now`` in ``[0.0, 1.0]``.

    ``resets_at`` is the moment the *current* window closes. The window
    opened ``window_s`` before that. Computes::

        elapsed = 1 - (resets_at - now) / window_s

    Clamped to ``[0, 1]`` — clock drift in either direction can't push
    the value outside that range.

    Raises :class:`ValueError` when ``window_s <= 0``.
    """
    if window_s <= 0:
        raise ValueError("window_s must be positive")
    remaining = (resets_at - now).total_seconds()
    raw = 1.0 - remaining / window_s
    return max(0.0, min(1.0, raw))


def target_pct(
    elapsed: float,
    *,
    early_pct: float,
    eow_pct: float,
    eow_window_fraction: float,
) -> float:
    """Target utilization (%) at a given elapsed fraction of the weekly window.

    Piecewise linear with breakpoint at ``B = 1 - eow_window_fraction``:

    * ``0 ≤ elapsed ≤ B``: ramp from ``0`` to ``early_pct``.
    * ``B < elapsed ≤ 1``: ramp from ``early_pct`` to ``eow_pct``.

    Defensive clamps:

    * ``elapsed`` clamps to ``[0, 1]``.
    * ``eow_window_fraction`` clamps to ``[0, 1]``. ``0`` collapses the
      EOW segment (curve ramps 0 → ``early_pct`` over the full week,
      ``eow_pct`` unused). ``1`` collapses the pre-EOW segment (curve
      ramps 0 → ``eow_pct`` over the full week; ``early_pct`` unused).

    Output clamps to ``[0, 100]``.
    """
    t = max(0.0, min(1.0, elapsed))
    f = max(0.0, min(1.0, eow_window_fraction))
    b = 1.0 - f
    if f == 0.0:
        value = t * early_pct
    elif f == 1.0:
        value = t * eow_pct
    elif t <= b:
        value = (t / b) * early_pct
    else:
        value = early_pct + ((t - b) / f) * (eow_pct - early_pct)
    return max(0.0, min(100.0, value))


def elapsed_for_target_pct(
    observed_pct: float,
    *,
    early_pct: float,
    eow_pct: float,
    eow_window_fraction: float,
) -> float:
    """Inverse of :func:`target_pct`.

    Returns the smallest ``t ∈ [0, 1]`` such that
    ``target_pct(t) ≥ observed_pct`` — i.e. the elapsed fraction at
    which the curve catches up to ``observed_pct``.

    Edge cases:

    * ``observed_pct ≥ eow_pct``: returns ``1.0``. The curve maxes out
      at ``eow_pct``; if observed is already above the cap, the
      supervisor should wake at week reset and reclassify.
    * ``observed_pct ≤ 0``: returns ``0.0``.

    Degenerate ``eow_window_fraction`` cases mirror those in
    :func:`target_pct`.
    """
    if observed_pct >= eow_pct:
        return 1.0
    if observed_pct <= 0.0:
        return 0.0

    f = max(0.0, min(1.0, eow_window_fraction))
    b = 1.0 - f

    if f == 0.0:
        # All segment A across the full week; curve hits early_pct at t=1.
        if early_pct == 0.0:
            return 1.0
        return min(1.0, observed_pct / early_pct)

    if f == 1.0:
        # All segment B across the full week; curve hits eow_pct at t=1.
        if eow_pct == 0.0:
            return 1.0
        return min(1.0, observed_pct / eow_pct)

    if observed_pct <= early_pct:
        # Segment A: observed = (t / B) * early_pct → t = observed * B / early_pct.
        if early_pct == 0.0:
            # Segment A is flat at 0 here; observed > 0 was caught above.
            return 0.0
        return observed_pct * b / early_pct

    # Segment B: observed = early_pct + ((t - B) / f) * (eow_pct - early_pct)
    #          → t = B + f * (observed - early_pct) / (eow_pct - early_pct).
    if eow_pct == early_pct:
        # Flat segment B; observed > early_pct unreachable.
        return 1.0
    return b + f * (observed_pct - early_pct) / (eow_pct - early_pct)


__all__ = [
    "SEVEN_DAYS_S",
    "elapsed_for_target_pct",
    "elapsed_fraction",
    "target_pct",
]
