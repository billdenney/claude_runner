"""Pure decision function: ``decide(policy, reading, clock) → Decision``.

Implements the ADR-0022 trace-following rule. Called once per
supervisor tick by :func:`supervisor.state_machine.step`. No I/O, no
global state — every input is explicit.

Decision order (per ADR-0022):

1. **Weekly first.** If ``observed_weekly > target_pct(elapsed_now)``,
   the result is ``THROTTLED_WEEKLY`` and dispatch halts. Wakeup is
   the analytical catch-up time (when the curve rises to meet
   observed), clamped to the next 5h reset (so the operator sees a
   familiar horizon in ``runner-status``) and to ``now +
   poll_interval_s`` (so the supervisor never busy-spins).
2. **Then 5h.** Pick day or night band by local time-of-day. Compare
   observed 5h to the band's thresholds:
     * ``≥ fivehr_stop_pct`` → ``THROTTLED_5H``.
     * ``≥ fivehr_slowdown_pct`` → ``SLOWING_DOWN`` with a linear
       concurrency ramp from ``max_concurrency`` (at slowdown) to ``0``
       (at stop).
     * else → ``DISPATCHING`` at ``max_concurrency``.

In-flight tasks are NEVER killed by the decision — the
``target_concurrency`` only gates *new* dispatches.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from claude_task_runner.clock import Clock
from claude_task_runner.supervisor.states import SupervisorState
from claude_task_runner.throttle import curve as _curve
from claude_task_runner.throttle import time_of_day as _tod
from claude_task_runner.throttle.policy import ResolvedPolicy
from claude_task_runner.usage.models import UsageReading

FIVE_HOUR_LENGTH_S: float = 5.0 * 3600.0
"""Nominal length of the 5-hour window. Fallback when ``resets_at`` is
unparseable."""


@dataclass(frozen=True)
class Decision:
    """Output of :func:`decide`.

    ``state`` and ``target_concurrency`` drive the supervisor's
    immediate action; ``wakeup_at`` schedules the next reclassification
    when the supervisor goes idle.

    The diagnostic fields (``target_pct``, ``observed_*``, ``band``,
    ``slowdown_pct``, ``stop_pct``) drive event payloads and the
    operator-facing status messages — keeping them on the result
    rather than recomputing in the wrapper keeps the math centralised.
    """

    state: SupervisorState
    target_concurrency: int
    wakeup_at: datetime | None
    message: str
    target_pct: float | None
    """Curve value at the current elapsed fraction; ``None`` only when
    ``reading.seven_day.resets_at`` is unparseable so the curve has
    nothing to anchor to."""
    observed_5h_pct: int
    observed_weekly_pct: int
    band: str
    """Which 5h band was active: ``"day"`` or ``"night"``."""
    fivehr_slowdown_pct: int
    fivehr_stop_pct: int


def _next_5h_reset_wakeup(
    reading: UsageReading,
    now: datetime,
    *,
    window_start_delay_s: float,
) -> datetime:
    """Wakeup time just past the next 5h reset.

    Falls back to ``now + FIVE_HOUR_LENGTH_S + delay`` when
    ``resets_at`` is unparseable.
    """
    if reading.five_hour.resets_at is not None:
        return reading.five_hour.resets_at + timedelta(seconds=window_start_delay_s)
    return now + timedelta(seconds=FIVE_HOUR_LENGTH_S + window_start_delay_s)


def _weekly_catch_up_at(
    *,
    observed_pct: float,
    resets_at: datetime,
    policy_week_early_pct: int,
    policy_week_eow_pct: int,
    eow_window_fraction: float,
    weekly_window_s: float,
) -> datetime:
    """When does ``target_pct(t')`` rise to meet ``observed_pct``?"""
    t_target = _curve.elapsed_for_target_pct(
        observed_pct,
        early_pct=float(policy_week_early_pct),
        eow_pct=float(policy_week_eow_pct),
        eow_window_fraction=eow_window_fraction,
    )
    return resets_at - timedelta(seconds=(1.0 - t_target) * weekly_window_s)


def _linear_ramp(
    *,
    observed_pct: int,
    slowdown_pct: int,
    stop_pct: int,
    max_concurrency: int,
) -> int:
    """Concurrency target between slowdown and stop bands.

    Same shape as the superseded ``runner.concurrency._target_for_band``:
    linear ramp from ``max_concurrency`` at ``slowdown_pct`` to ``0`` at
    ``stop_pct``, ``ceil`` rounded.
    """
    span = stop_pct - slowdown_pct
    if span <= 0:
        return 0
    progress = (observed_pct - slowdown_pct) / span
    progress = max(0.0, min(1.0, progress))
    target = math.ceil(max_concurrency * (1.0 - progress))
    return max(0, min(max_concurrency, target))


def decide(
    policy: ResolvedPolicy,
    reading: UsageReading,
    clock: Clock,
    *,
    poll_interval_s: float,
    window_start_delay_s: float = 0.0,
    weekly_window_s: float = _curve.SEVEN_DAYS_S,
) -> Decision:
    """Trace-following dispatch decision (ADR-0022).

    Parameters
    ----------
    policy
        Merged per-account policy.
    reading
        Clean :class:`UsageReading` (callers route exceptions to
        ERROR_DRIFT before this is invoked).
    clock
        Injected :class:`Clock` so the function is fully testable.
    poll_interval_s
        Lower bound on any computed wakeup — prevents busy-spin when
        the analytical catch-up has already passed.
    window_start_delay_s
        Padding added to 5h reset wakeups (matches
        ``[supervisor].window_start_delay_s``).
    weekly_window_s
        Length of the weekly window; defaults to
        :data:`curve.SEVEN_DAYS_S`. Exposed for tests.
    """
    now = clock.now()
    observed_5h = reading.five_hour.utilization_pct
    observed_weekly = reading.seven_day.utilization_pct

    # ---- Weekly check ---------------------------------------------------
    weekly_target: float | None = None
    weekly_throttled = False
    weekly_catch_up: datetime | None = None
    if reading.seven_day.resets_at is not None:
        elapsed = _curve.elapsed_fraction(
            now, reading.seven_day.resets_at, window_s=weekly_window_s
        )
        eow_window_fraction = (
            policy.week.eow_time_switch_s / weekly_window_s if weekly_window_s > 0 else 0.0
        )
        weekly_target = _curve.target_pct(
            elapsed,
            early_pct=float(policy.week.early_pct),
            eow_pct=float(policy.week.eow_pct),
            eow_window_fraction=eow_window_fraction,
        )
        if float(observed_weekly) > weekly_target:
            weekly_throttled = True
            weekly_catch_up = _weekly_catch_up_at(
                observed_pct=float(observed_weekly),
                resets_at=reading.seven_day.resets_at,
                policy_week_early_pct=policy.week.early_pct,
                policy_week_eow_pct=policy.week.eow_pct,
                eow_window_fraction=eow_window_fraction,
                weekly_window_s=weekly_window_s,
            )

    # ---- 5h band selection ---------------------------------------------
    now_local = _tod.to_local(now, policy.timezone)
    band_name = _tod.which_band(
        now_local,
        night_start=policy.night.time_start,
        night_end=policy.night.time_end,
    )
    band = policy.night if band_name == "night" else policy.day
    slow_pct = band.fivehr_slowdown_pct
    stop_pct = band.fivehr_stop_pct

    five_h_reset_wakeup = _next_5h_reset_wakeup(
        reading, now, window_start_delay_s=window_start_delay_s
    )
    min_wakeup = now + timedelta(seconds=poll_interval_s)

    # ---- Decision -------------------------------------------------------
    if weekly_throttled:
        # Clamp: never sleep past the next 5h reset, never busy-spin.
        candidate = weekly_catch_up
        assert candidate is not None  # weekly_throttled implies resets_at present
        wakeup = max(min_wakeup, min(five_h_reset_wakeup, candidate))
        message = (
            f"weekly utilization {observed_weekly}% > target "
            f"{weekly_target:.1f}%; pausing dispatch until trace catches up"
        )
        return Decision(
            state=SupervisorState.THROTTLED_WEEKLY,
            target_concurrency=0,
            wakeup_at=wakeup,
            message=message,
            target_pct=weekly_target,
            observed_5h_pct=observed_5h,
            observed_weekly_pct=observed_weekly,
            band=band_name,
            fivehr_slowdown_pct=slow_pct,
            fivehr_stop_pct=stop_pct,
        )

    if observed_5h >= stop_pct:
        message = (
            f"5h utilization {observed_5h}% >= stop {stop_pct}% ({band_name}); "
            "pausing dispatch until next 5h reset"
        )
        return Decision(
            state=SupervisorState.THROTTLED_5H,
            target_concurrency=0,
            wakeup_at=max(min_wakeup, five_h_reset_wakeup),
            message=message,
            target_pct=weekly_target,
            observed_5h_pct=observed_5h,
            observed_weekly_pct=observed_weekly,
            band=band_name,
            fivehr_slowdown_pct=slow_pct,
            fivehr_stop_pct=stop_pct,
        )

    if observed_5h >= slow_pct:
        target_c = _linear_ramp(
            observed_pct=observed_5h,
            slowdown_pct=slow_pct,
            stop_pct=stop_pct,
            max_concurrency=policy.max_concurrency,
        )
        message = (
            f"slowing dispatch: 5h={observed_5h}% in [{slow_pct}, {stop_pct}) "
            f"({band_name}); target concurrency={target_c}/"
            f"{policy.max_concurrency}"
        )
        return Decision(
            state=SupervisorState.SLOWING_DOWN,
            target_concurrency=target_c,
            wakeup_at=max(min_wakeup, five_h_reset_wakeup),
            message=message,
            target_pct=weekly_target,
            observed_5h_pct=observed_5h,
            observed_weekly_pct=observed_weekly,
            band=band_name,
            fivehr_slowdown_pct=slow_pct,
            fivehr_stop_pct=stop_pct,
        )

    return Decision(
        state=SupervisorState.DISPATCHING,
        target_concurrency=policy.max_concurrency,
        wakeup_at=None,
        message="",
        target_pct=weekly_target,
        observed_5h_pct=observed_5h,
        observed_weekly_pct=observed_weekly,
        band=band_name,
        fivehr_slowdown_pct=slow_pct,
        fivehr_stop_pct=stop_pct,
    )


__all__ = [
    "FIVE_HOUR_LENGTH_S",
    "Decision",
    "decide",
]
