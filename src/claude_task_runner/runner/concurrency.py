"""Three-band dispatch policy with EMA-driven target concurrency.

Pure module — given current usage, settings, and the EMA, returns a
:class:`DispatchDecision` describing what the supervisor should do this
tick. No subprocess spawning, no I/O.

Bands (cutoffs in :class:`ThrottleBandSettings`):

==============  ==================================  =========================
``predicted``   Behavior                             :class:`DispatchBand`
==============  ==================================  =========================
``< 70%``       Dispatch up to ``max_concurrency``   ``DispatchBand.FULL``
``70-90%``      Linear slowdown (see below)          ``DispatchBand.SLOW``
``>= 90%``      No new dispatch                      ``DispatchBand.STOPPED``
==============  ==================================  =========================

Linear slowdown formula::

    target = ceil(max_concurrency * (1 - (predicted_pct - 0.70) / 0.20))
    target = clamp(target, 0, max_concurrency)

The same shape applies to the weekly window. The more restrictive of
the two windows wins — :func:`compute_target_concurrency` returns
``min(target_5h, target_weekly)``.

See ADR-0004 (three-band throttle) and ADR-0011 (EMA-driven concurrency).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from claude_task_runner.config.schema import (
    ConcurrencySettings,
    EMASettings,
    ThrottleBandSettings,
    ThrottleSettings,
    ThrottleWeeklySettings,
)
from claude_task_runner.queue.schema import Task
from claude_task_runner.runner import ema as ema_mod
from claude_task_runner.runner.ema import EMAFile


class DispatchBand(StrEnum):
    """Which throttle band the predicted utilization falls in."""

    FULL = "full"
    SLOW = "slow"
    STOPPED = "stopped"


@dataclass(frozen=True)
class DispatchDecision:
    """The output of a per-tick concurrency decision.

    Attributes
    ----------
    band
        Most restrictive band hit between 5h and weekly.
    target_concurrency
        How many tasks may be running concurrently. ``0`` means "no new
        dispatches" (in-flight tasks continue to completion).
    five_hour_band, weekly_band
        Per-window classifications for telemetry.
    predicted_5h_pct, predicted_weekly_pct
        The values that drove the decision, expressed as fractions in
        ``[0.0, 1.0+]``. Useful for logs.
    """

    band: DispatchBand
    target_concurrency: int
    five_hour_band: DispatchBand
    weekly_band: DispatchBand
    predicted_5h_pct: float
    predicted_weekly_pct: float


def _band_for(pct: float, settings: ThrottleBandSettings) -> DispatchBand:
    """Classify a predicted utilization fraction into a band."""
    full_max = settings.band_full_dispatch_max_pct / 100.0
    slow_max = settings.band_slowdown_max_pct / 100.0
    if pct < full_max:
        return DispatchBand.FULL
    if pct < slow_max:
        return DispatchBand.SLOW
    return DispatchBand.STOPPED


def _target_for_band(
    band: DispatchBand,
    pct: float,
    *,
    max_concurrency: int,
    settings: ThrottleBandSettings,
) -> int:
    """Compute target concurrency given the band classification."""
    if band is DispatchBand.FULL:
        return max_concurrency
    if band is DispatchBand.STOPPED:
        return 0
    # Linear slowdown
    full_max = settings.band_full_dispatch_max_pct / 100.0
    slow_max = settings.band_slowdown_max_pct / 100.0
    span = slow_max - full_max
    if span <= 0:
        # Degenerate config (full_max >= slow_max). Treat as STOPPED in
        # the slow band; pure-function safety net for misconfiguration.
        return 0
    progress = (pct - full_max) / span  # in [0, 1]
    progress = max(0.0, min(1.0, progress))
    target = math.ceil(max_concurrency * (1.0 - progress))
    return max(0, min(max_concurrency, target))


def predict_post_dispatch_pct(
    *,
    used_tokens: int,
    in_flight_estimate_tokens: float,
    new_task_estimate_tokens: float,
    budget_tokens: int,
) -> float:
    """Compute the predicted utilization fraction after dispatching one
    more task.

    Returns ``(used + in_flight + new) / budget``. Caller is responsible
    for what counts as ``in_flight_estimate_tokens`` (sum of EMA estimates
    for currently-running tasks).
    """
    if budget_tokens <= 0:
        raise ValueError("budget_tokens must be > 0")
    return (
        float(used_tokens) + float(in_flight_estimate_tokens) + float(new_task_estimate_tokens)
    ) / float(budget_tokens)


def compute_target_concurrency(
    *,
    used_5h_tokens: int,
    used_weekly_tokens: int,
    in_flight_estimate_tokens: float,
    new_task_estimate_tokens: float,
    five_hour: ThrottleBandSettings,
    weekly: ThrottleWeeklySettings,
    concurrency: ConcurrencySettings,
    have_ema_warmup: bool,
) -> DispatchDecision:
    """Compute the dispatch decision for the next tick.

    The caller passes the most-restrictive **window-utilization
    fraction** by populating both windows. Whichever produces a lower
    target concurrency wins (per ADR-0004).

    ``have_ema_warmup`` toggles between ``initial_concurrency`` (until
    we have ``ema.prior_warmup_samples`` real observations) and
    ``max_concurrency`` (after).
    """
    max_c = concurrency.max_concurrency if have_ema_warmup else concurrency.initial_concurrency
    if max_c < 1:
        max_c = 1

    pct_5h = predict_post_dispatch_pct(
        used_tokens=used_5h_tokens,
        in_flight_estimate_tokens=in_flight_estimate_tokens,
        new_task_estimate_tokens=new_task_estimate_tokens,
        budget_tokens=five_hour.budget_tokens,
    )
    pct_weekly = predict_post_dispatch_pct(
        used_tokens=used_weekly_tokens,
        in_flight_estimate_tokens=in_flight_estimate_tokens,
        new_task_estimate_tokens=new_task_estimate_tokens,
        budget_tokens=weekly.budget_tokens,
    )

    band_5h = _band_for(pct_5h, five_hour)
    band_weekly = _band_for(pct_weekly, weekly)

    target_5h = _target_for_band(band_5h, pct_5h, max_concurrency=max_c, settings=five_hour)
    target_weekly = _target_for_band(
        band_weekly, pct_weekly, max_concurrency=max_c, settings=weekly
    )

    # Most-restrictive wins.
    target = min(target_5h, target_weekly)
    winning_band = band_5h if target_5h <= target_weekly else band_weekly

    return DispatchDecision(
        band=winning_band,
        target_concurrency=target,
        five_hour_band=band_5h,
        weekly_band=band_weekly,
        predicted_5h_pct=pct_5h,
        predicted_weekly_pct=pct_weekly,
    )


def in_flight_estimate_tokens(
    in_flight_tasks: list[Task],
    ema: EMAFile,
    *,
    ema_settings: EMASettings,
) -> float:
    """Sum predicted token costs across a set of in-flight tasks."""
    return sum(ema_mod.predict_tokens(ema, t, settings=ema_settings) for t in in_flight_tasks)


def should_dispatch(
    *,
    candidate: Task,
    in_flight_tasks: list[Task],
    used_5h_tokens: int,
    used_weekly_tokens: int,
    ema: EMAFile,
    settings_throttle: ThrottleSettings,
    settings_concurrency: ConcurrencySettings,
    settings_ema: EMASettings,
    have_ema_warmup: bool,
) -> tuple[bool, DispatchDecision]:
    """Decide whether to dispatch ``candidate`` right now.

    Returns ``(should_dispatch, decision)`` so callers can log the
    decision regardless of outcome. ``should_dispatch=True`` only when
    the predicted post-dispatch state stays below the
    no-dispatch threshold AND running task count would not exceed the
    target concurrency.
    """
    new_estimate = ema_mod.predict_tokens(ema, candidate, settings=settings_ema)
    in_flight_est = in_flight_estimate_tokens(in_flight_tasks, ema, ema_settings=settings_ema)

    decision = compute_target_concurrency(
        used_5h_tokens=used_5h_tokens,
        used_weekly_tokens=used_weekly_tokens,
        in_flight_estimate_tokens=in_flight_est,
        new_task_estimate_tokens=new_estimate,
        five_hour=settings_throttle.five_hour,
        weekly=settings_throttle.weekly,
        concurrency=settings_concurrency,
        have_ema_warmup=have_ema_warmup,
    )

    if decision.band is DispatchBand.STOPPED:
        return False, decision
    if len(in_flight_tasks) >= decision.target_concurrency:
        return False, decision
    return True, decision
