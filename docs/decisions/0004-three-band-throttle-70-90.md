# ADR-0004: Three-band throttle (70/90) with EMA prediction

- **Date:** 2026-05-03
- **Status:** accepted; amended by ADR-0015 (time-of-day modulation layered on top)

## Context

The runner needs a dispatch decision policy that uses up most of the
5-hour budget without crashing into the wall, and lets in-flight tasks
finish naturally even when the budget is consumed.

## Decision

Three throttle bands keyed off **predicted** post-dispatch utilization,
where prediction = `(used + sum(in_flight_estimates) + new_task_estimate)
/ budget`. All cutoffs are TOML settings.

| Predicted | Behavior | State |
|---|---|---|
| < 70% | Full target concurrency | Dispatching |
| 70–90% | Linear slowdown: `target = ceil(max * (1 - (pred - 0.70) / 0.20))` | SlowingDown |
| ≥ 90% | No new dispatch | Throttled5h |

The same shape applies to the weekly window with its own thresholds. The
more restrictive throttle wins.

## Alternatives considered

- **Single hard threshold at 90%:** rejected because it gives no headroom
  for in-flight task overruns and wastes the 70–90% band that could
  productively dispatch shorter tasks.
- **Two-band (full / no-dispatch):** same problem; nothing dispatches when
  predicted hits the band edge, even though shorter tasks would still fit.
- **Hard stop at 95% with kill of in-flight:** rejected because killed
  in-flight work has to be re-paid in the next window.

## Consequences

- (+) Aggressive utilization without crashing: aim for 90%, never exceed.
- (+) In-flight tasks always finish naturally.
- (-) Requires reasonable EMA estimates to avoid flapping. Mitigated by
  cold-start priors per `(model, effort)` and `prior_warmup_samples`.

## Reversibility

High. Cutoffs are TOML settings; an operator can revert to single-threshold
behavior by setting `band_full_dispatch_max_pct = band_slowdown_max_pct`.
