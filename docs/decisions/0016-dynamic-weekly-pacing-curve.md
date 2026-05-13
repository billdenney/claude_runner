# ADR-0016: Dynamic weekly pacing curve anchored to OAuth reset

- **Date:** 2026-05-13
- **Status:** accepted

## Context

ADR-0006 introduced a hard pause at `[throttle.weekly].pause_at_pct`
(default 90%) plus an end-of-week push window. In practice on the
operator's queues the runner reaches 90% by mid-week, sits in
PausedWeekly until the EOW window opens, then burns through the
remaining 8% in the last 12 hours. This produces three problems:

1. **Front-loaded consumption.** The first half of the week gets the
   full budget; the second half gets none. If the operator's queue
   ingests new work later in the week, it has nothing to spend.
2. **Wasted EOW window.** Some weeks the push window's runtime guard
   refuses tasks (no EMA samples short enough); the 8% goes unused.
3. **Fixed-weekday reset assumption is wrong.** Anthropic's weekly
   window is *rolling*: in one observed week the reset shifted to
   07:00 on Wednesday. Code that anchored to "Monday 00:00 UTC" (or
   any fixed weekday) systematically miscomputes elapsed-in-week.

## Decision

Add a **dynamic pacing curve** that shifts the effective weekly bands
based on how far observed utilization deviates from a target curve at
the current elapsed fraction of the weekly window. The window is
anchored to `UsageReading.seven_day.resets_at` — the OAuth-reported
reset timestamp — not to any fixed weekday.

New settings on `[throttle.weekly]`:

```toml
pacing_curve_enabled = true
pre_eow_target_pct   = 80     # target utilization at start of EOW window
pacing_slack_pp      = 10.0   # dead-band ± N pp around target curve
```

Existing `eow_target_pct` is lowered 98 → 95 (leave headroom against
a final-tick burst); `eow_window_s` widened 12h → 24h (more
opportunities for the nighttime-biased EOW push from ADR-0015).

### Curve shape

Piecewise linear with two segments. Let `t = elapsed_fraction`,
`E = eow_window_fraction = eow_window_s / 7d`, `P = pre_eow_target_pct`,
`F = eow_target_pct`:

* `0 ≤ t ≤ 1 − E`: ramp linearly from 0 to P.
* `1 − E < t ≤ 1`: ramp linearly from P to F.

With defaults `(P=80, F=95, E≈0.143)` this gives:

| `t` (elapsed) | Target % |
|---|---|
| 0.0 | 0 |
| 0.25 | 23.3 |
| 0.5 | 46.7 |
| 0.857 (EOW window opens) | 80 |
| 0.93 (mid-EOW) | 87.5 |
| 1.0 (reset) | 95 |

### Band adjustment

At each tick, `adjusted_weekly_band` shifts each static band by the
observed-vs-target deviation **outside** the `pacing_slack_pp`
dead-band:

* If `|observed − target| ≤ slack`: bands unchanged.
* If `observed > target + slack`: bands shift **down** by
  `(observed − target) − slack` percentage points (tighter — slow the
  burn).
* If `observed < target − slack`: bands shift **up** by
  `(target − observed) − slack` percentage points (looser — let the
  queue catch up).

Adjustment is clamped to `[0, pause_at_pct]` — the hard pause floor is
never overridden. `pause_at_pct` itself is read directly from settings
and is independent of the curve.

## Alternatives considered

* **Quadratic curve `target = F · t²`.** Single-knob, intuitive,
  implicitly slow-starts. Rejected because it complicates the
  end-of-week burst: a quadratic that hits 95% at `t=1` is at ~67% at
  `t=0.85`, leaving only 28% to spend in the last 15% of the week,
  not the 15% the operator wanted reserved for the nighttime EOW
  push. The piecewise-linear shape makes that reservation explicit.
* **Fixed daily cap `weekly_target / 7` per day.** Doesn't survive
  Anthropic's variable reset day; also doesn't accommodate the
  operator's preference for an EOW burst.
* **Always pause until reset (status quo of ADR-0006 without the
  curve).** Wastes the daytime budget that the curve can productively
  use.
* **Quotient regulation (continuous PID-style).** Overkill for the
  poll cadence (60s) and the natural granularity of `utilization_pct`
  (integer percentages from the OAuth API). The dead-band linear
  shift is enough.

## Consequences

* (+) Weekly consumption tracks a known target curve; the operator
  can read the per-tick "ahead/behind by X pp" off the status display.
* (+) The curve is robust to rolling reset windows: it always anchors
  to the current `resets_at`.
* (+) Falls back gracefully when `resets_at` is unparseable (returns
  the static bands).
* (+) `pacing_curve_enabled = false` reproduces ADR-0006 behavior
  exactly.
* (-) Adds one more knob (`pacing_slack_pp`) that operators must
  understand. Mitigated by the cheat sheet's "when to bump/lower"
  table and a default that hardly anyone needs to change.
* (-) The curve makes weekly band thresholds dynamic, which makes the
  status display less directly readable: "70% of weekly used" no
  longer maps 1:1 to "70%-band". The supervisor emits
  `state_transition` events with the effective bands so the operator
  can audit.

## Reversibility

High. Per-queue: `pacing_curve_enabled = false` reverts to ADR-0006.
The new fields all have schema defaults so an existing TOML without
them validates without change.

Amendments: this ADR amends ADR-0006 (adds a dynamic layer on top of
the static pause-and-push behavior). ADR-0006's decision stays
authoritative when the pacing curve is disabled.
