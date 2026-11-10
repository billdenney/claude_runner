"""Piecewise-linear weekly target curve and its analytical inverse.

The supervisor's weekly trace-following decision rule (ADR-0022) is:

* `target_pct(elapsed)` rises from 0 to ``early_pct`` over the pre-EOW
  segment, then from ``early_pct`` to ``eow_pct`` over the EOW segment
  (the last ``eow_window_fraction`` of the week).
* If observed > target, the supervisor stops weekly dispatch and wakes
  when the target catches up — solved analytically via
  :func:`elapsed_for_target_pct`.

``early_pct = 0`` gives the curve a different shape (ADR-0041): no
target before the EOW segment, and a flat ``eow_pct`` throughout it,
so the account takes tasks only in the last ``eow_window_fraction`` of
the week. :func:`window_open` says where that holds dispatch closed.

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


def end_of_week_only(early_pct: float) -> bool:
    """Whether the curve confines dispatch to the EOW segment (ADR-0041).

    ``early_pct = 0`` sets no target before the segment, so the account
    takes tasks only in the last ``eow_time_switch`` before the weekly
    reset, up to ``eow_pct``.
    """
    return early_pct == 0.0


def window_open(elapsed: float, *, early_pct: float, eow_window_fraction: float) -> bool:
    """Whether the weekly curve lets the account take tasks at all at ``elapsed``.

    Always ``True`` for a sloped curve, whose comparison of observed with
    target decides alone. For an end-of-week-only curve
    (:func:`end_of_week_only`) it is ``True`` only inside the EOW segment,
    from ``1 - eow_window_fraction`` up to the reset: before it, the target
    is 0 and a reading of 0% must not dispatch either.

    ``elapsed = 1`` is the reset itself, or a reading whose reset has
    passed (:func:`elapsed_fraction` clamps), so a new week has begun and
    its window is closed. A zero-length segment never opens; a segment of
    the whole window never closes.
    """
    if not end_of_week_only(early_pct):
        return True
    t = max(0.0, min(1.0, elapsed))
    f = max(0.0, min(1.0, eow_window_fraction))
    if f == 1.0:
        return True
    return f > 0.0 and 1.0 - f <= t < 1.0


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

    ``early_pct = 0`` is a step instead (ADR-0041): ``0`` while
    :func:`window_open` is ``False``, ``eow_pct`` while it is ``True``.

    Output clamps to ``[0, 100]``.
    """
    t = max(0.0, min(1.0, elapsed))
    f = max(0.0, min(1.0, eow_window_fraction))
    b = 1.0 - f
    if end_of_week_only(early_pct):
        value = eow_pct if window_open(t, early_pct=early_pct, eow_window_fraction=f) else 0.0
    elif f == 0.0:
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

    Edge cases of a sloped curve:

    * ``observed_pct ≥ eow_pct``: returns ``1.0``. The curve maxes out
      at ``eow_pct``; if observed is already above the cap, the
      supervisor should wake at week reset and reclassify.
    * ``observed_pct ≤ 0``: returns ``0.0``.

    Degenerate ``eow_window_fraction`` cases mirror those in
    :func:`target_pct`.

    For an end-of-week-only curve (``early_pct = 0``, ADR-0041) the
    answer is when the EOW segment opens, ``1 - eow_window_fraction``,
    for every observed value up to and including ``eow_pct``, since the
    step reaches ``eow_pct`` there. Above ``eow_pct``, or with a
    zero-length segment that never opens, it is ``1.0``, the reset.
    """
    f = max(0.0, min(1.0, eow_window_fraction))
    b = 1.0 - f

    if end_of_week_only(early_pct):
        return 1.0 if f == 0.0 or observed_pct > eow_pct else b

    if observed_pct >= eow_pct:
        return 1.0

    if observed_pct <= 0.0:
        return 0.0

    if f == 0.0:
        # All segment A across the full week; curve hits early_pct at t=1.
        return min(1.0, observed_pct / early_pct)

    if f == 1.0:
        # All segment B across the full week; curve hits eow_pct at t=1.
        if eow_pct == 0.0:  # pragma: no cover
            # Unreachable: ``observed_pct >= eow_pct`` short-circuits at
            # the top of the function, so eow_pct == 0 + observed_pct > 0
            # cannot reach this branch. Kept as a defensive guard.
            return 1.0
        return min(1.0, observed_pct / eow_pct)

    if observed_pct <= early_pct:
        # Segment A: observed = (t / B) * early_pct → t = observed * B / early_pct.
        if early_pct == 0.0:  # pragma: no cover
            # Unreachable: ``observed_pct <= 0`` short-circuits at the top
            # so observed_pct <= 0 + early_pct == 0 cannot reach here.
            # Kept as a defensive guard.
            return 0.0
        return observed_pct * b / early_pct

    # Segment B: observed = early_pct + ((t - B) / f) * (eow_pct - early_pct)
    #          → t = B + f * (observed - early_pct) / (eow_pct - early_pct).
    if eow_pct == early_pct:  # pragma: no cover
        # Unreachable: ``observed_pct >= eow_pct`` short-circuits at the
        # top of the function, so eow_pct == early_pct + observed_pct >
        # early_pct cannot reach here. Kept as a defensive guard.
        return 1.0
    return b + f * (observed_pct - early_pct) / (eow_pct - early_pct)


__all__ = [
    "SEVEN_DAYS_S",
    "elapsed_for_target_pct",
    "elapsed_fraction",
    "end_of_week_only",
    "target_pct",
    "window_open",
]
