# ADR-0006: Pause weekly at threshold + end-of-week push

- **Date:** 2026-05-03
- **Status:** accepted; amended by ADR-0015 (nighttime-biased EOW push) and ADR-0016 (dynamic weekly pacing curve)

## Context

The 7-day window is a separate budget from the 5-hour window. When the
weekly budget is near exhausted, dispatching aggressively risks hitting
the wall mid-task. But sitting idle until reset wastes any remaining
budget that could complete short tasks.

## Decision

Two-stage weekly behavior:

1. **PausedWeekly**: when weekly utilization crosses
   `[throttle.weekly].pause_at_pct`, halt dispatch and emit a desktop
   notification.
2. **EndOfWeekPush**: when paused AND `time_until_weekly_reset <
   eow_window_s` (default 12h) AND weekly utilization is below
   `eow_target_pct` (default 98%), re-enter dispatch but only for tasks
   whose p90 historical runtime is ≤ `eow_runtime_safety_factor` ×
   `time_until_reset`. Unknown task types (no EMA samples) are refused
   unless the task has `force_dispatch_in_eow: true`.

The runtime guard is critical: a task that runs into the next week bills
the new week's budget at full speed, defeating the purpose.

## Alternatives considered

- **Always pause until reset:** wastes 2-10% of weekly budget.
- **Push aggressively without runtime guard:** dangerous — long tasks
  bleed into next week.
- **Hard cost cap (USD/week):** offered to user, declined for v1.

## Consequences

- (+) Captures value from leftover weekly budget.
- (+) Guards against overruns into next week.
- (-) Requires EMA history per task type to be useful. Cold-start case is
  handled by `force_dispatch_in_eow` and conservative priors.

## Reversibility

High. Set `eow_window_s = 0` to disable end-of-week push entirely.
