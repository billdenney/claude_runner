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
   With ``early_pct = 0`` (ADR-0041) the account is also
   ``THROTTLED_WEEKLY`` whenever the end-of-week window is closed,
   whatever it has used, and wakes when the window opens.
2. **Then 5h.** Pick day or night band by local time-of-day. Compare
   observed 5h to the band's thresholds:
     * ``≥ fivehr_stop_pct`` → ``THROTTLED_5H``.
     * ``≥ fivehr_slowdown_pct`` → ``SLOWING_DOWN`` with a linear
       concurrency ramp from ``max_concurrency`` (at slowdown) to ``0``
       (at stop).
     * else → ``DISPATCHING`` at ``max_concurrency``.

A reading with no 5-hour window (``reading.five_hour is None``,
ADR-0038) skips step 2: the 5h thresholds do not apply, so the weekly
check alone decides between ``THROTTLED_WEEKLY`` and ``DISPATCHING``,
and a weekly wakeup is the catch-up time with no 5h reset to clamp it.

In-flight tasks are NEVER killed by the decision — the
``target_concurrency`` only gates *new* dispatches. The state machine
records it on the account's :class:`supervisor.states.AccountState`, and
:func:`runner.account_dispatch.choose_account` caps that account's
in-flight tasks at it.
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
from claude_task_runner.usage.models import UsageReading, WindowReading

FIVE_HOUR_LENGTH_S: float = 5.0 * 3600.0
"""Nominal length of the 5-hour window. Fallback when ``resets_at`` is
unparseable."""


@dataclass(frozen=True)
class Decision:
    """Output of :func:`decide`.

    ``state`` and ``target_concurrency`` drive the supervisor's
    immediate action: both land on the account's state, and dispatch
    through the account stops, or is capped at ``target_concurrency``,
    accordingly. ``wakeup_at`` schedules the next reclassification when
    the supervisor goes idle.

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
    observed_5h_pct: int | None
    """``None`` when the reading reported no 5-hour window."""
    observed_weekly_pct: int
    band: str
    """Which 5h band was active: ``"day"`` or ``"night"``. Its thresholds
    are reported even when the reading has no 5-hour window for them to
    apply to."""
    fivehr_slowdown_pct: int
    fivehr_stop_pct: int


def _next_5h_reset_wakeup(
    five_hour: WindowReading,
    now: datetime,
    *,
    window_start_delay_s: float,
) -> datetime:
    """Wakeup time just past the next 5h reset.

    Falls back to ``now + FIVE_HOUR_LENGTH_S + delay`` when
    ``resets_at`` is unparseable.
    """
    if five_hour.resets_at is not None:
        return five_hour.resets_at + timedelta(seconds=window_start_delay_s)
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


def _eow_window_fraction(policy: ResolvedPolicy, weekly_window_s: float) -> float:
    """The end-of-week segment's share of the weekly window."""
    return policy.week.eow_time_switch_s / weekly_window_s if weekly_window_s > 0 else 0.0


def weekly_catch_up(
    policy: ResolvedPolicy,
    *,
    observed_pct: float,
    resets_at: datetime,
    weekly_window_s: float = _curve.SEVEN_DAYS_S,
) -> datetime:
    """When the weekly target curve rises to ``observed_pct`` (ADR-0022).

    For a THROTTLED_WEEKLY account this is when it takes tasks again, as far
    as the reading it was throttled on goes: the supervisor re-checks
    earlier (its wakeup is clamped to the next 5h reset), and work still
    running can raise the observed figure. ``resets_at`` is the end of the
    current weekly window; observed at or above ``eow_pct`` gives the reset
    itself.
    """
    return _weekly_catch_up_at(
        observed_pct=observed_pct,
        resets_at=resets_at,
        policy_week_early_pct=policy.week.early_pct,
        policy_week_eow_pct=policy.week.eow_pct,
        eow_window_fraction=_eow_window_fraction(policy, weekly_window_s),
        weekly_window_s=weekly_window_s,
    )


def _window_closed_message(policy: ResolvedPolicy, resets_at: datetime) -> str:
    """Why an end-of-week-only account (``early_pct = 0``) takes no tasks yet."""
    switch_s = policy.week.eow_time_switch_s
    if switch_s <= 0:
        return (
            "early_pct = 0 takes tasks only in the last eow_time_switch before the "
            "weekly reset, and eow_time_switch is 0, so this account takes none"
        )
    opens = resets_at - timedelta(seconds=switch_s)
    return (
        f"early_pct = 0: waiting for the end-of-week window, the last "
        f"{switch_s / 3600:g}h before the weekly reset, which opens at "
        f"{opens.isoformat(timespec='minutes')}"
    )


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
    five_hour = reading.five_hour
    observed_5h = reading.five_hour_util_pct
    observed_weekly = reading.seven_day.utilization_pct

    # ---- Weekly check ---------------------------------------------------
    weekly_target: float | None = None
    weekly_throttled = False
    window_closed = False
    catch_up_at: datetime | None = None
    if reading.seven_day.resets_at is not None:
        elapsed = _curve.elapsed_fraction(
            now, reading.seven_day.resets_at, window_s=weekly_window_s
        )
        eow_window_fraction = _eow_window_fraction(policy, weekly_window_s)
        weekly_target = _curve.target_pct(
            elapsed,
            early_pct=float(policy.week.early_pct),
            eow_pct=float(policy.week.eow_pct),
            eow_window_fraction=eow_window_fraction,
        )
        window_closed = not _curve.window_open(
            elapsed,
            early_pct=float(policy.week.early_pct),
            eow_window_fraction=eow_window_fraction,
        )
        if window_closed or float(observed_weekly) > weekly_target:
            weekly_throttled = True
            catch_up_at = weekly_catch_up(
                policy,
                observed_pct=float(observed_weekly),
                resets_at=reading.seven_day.resets_at,
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

    min_wakeup = now + timedelta(seconds=poll_interval_s)

    # ---- Decision -------------------------------------------------------
    if weekly_throttled:
        # Clamp: never sleep past the next 5h reset, never busy-spin.
        candidate = catch_up_at
        assert candidate is not None  # weekly_throttled implies resets_at present
        if five_hour is not None:
            five_h_reset = _next_5h_reset_wakeup(
                five_hour, now, window_start_delay_s=window_start_delay_s
            )
            candidate = min(five_h_reset, candidate)
        wakeup = max(min_wakeup, candidate)
        if window_closed:
            assert reading.seven_day.resets_at is not None  # window_closed implies it
            message = _window_closed_message(policy, reading.seven_day.resets_at)
        else:
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

    # A reading with no 5-hour window has no 5h thresholds to apply, so it
    # dispatches at max_concurrency below.
    if five_hour is not None:
        five_h_pct = five_hour.utilization_pct
        five_h_wakeup = max(
            min_wakeup,
            _next_5h_reset_wakeup(five_hour, now, window_start_delay_s=window_start_delay_s),
        )
        if five_h_pct >= stop_pct:
            message = (
                f"5h utilization {five_h_pct}% >= stop {stop_pct}% ({band_name}); "
                "pausing dispatch until next 5h reset"
            )
            return Decision(
                state=SupervisorState.THROTTLED_5H,
                target_concurrency=0,
                wakeup_at=five_h_wakeup,
                message=message,
                target_pct=weekly_target,
                observed_5h_pct=five_h_pct,
                observed_weekly_pct=observed_weekly,
                band=band_name,
                fivehr_slowdown_pct=slow_pct,
                fivehr_stop_pct=stop_pct,
            )

        if five_h_pct >= slow_pct:
            target_c = _linear_ramp(
                observed_pct=five_h_pct,
                slowdown_pct=slow_pct,
                stop_pct=stop_pct,
                max_concurrency=policy.max_concurrency,
            )
            message = (
                f"slowing dispatch: 5h={five_h_pct}% in [{slow_pct}, {stop_pct}) "
                f"({band_name}); target concurrency={target_c}/"
                f"{policy.max_concurrency}"
            )
            return Decision(
                state=SupervisorState.SLOWING_DOWN,
                target_concurrency=target_c,
                wakeup_at=five_h_wakeup,
                message=message,
                target_pct=weekly_target,
                observed_5h_pct=five_h_pct,
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
    "weekly_catch_up",
]
