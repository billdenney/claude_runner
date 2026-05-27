# ADR-0015: Time-of-day band modulation for 5h throttle + nighttime-biased EOW push

- **Date:** 2026-05-13
- **Status:** superseded by ADR-0022

## Context

The operator uses Claude interactively during their workday for tasks
unrelated to the runner's queue. With the static 40/60 or 70/90 5h bands
from ADR-0004, the supervisor will dispatch up to the band threshold
regardless of whether the operator is currently working — frequently
exhausting the operator's interactive 5h headroom by mid-afternoon and
forcing them to stop using Claude until the next 5h window rolls over.

Token math anchors the magnitude of the problem. On a Max20x plan the
weekly cap (798M) is ≈ 8× the 5h cap (99M). A full week is 33.6 × 5h
windows. At the static 70%-of-5h "full dispatch" threshold, full-speed
dispatch would consume ~33.6 × 70% × 8 = 1880% of weekly budget per
week — wildly over. The static thresholds rely on the weekly cap to
brake (ADR-0006), which by that point has eaten all the daytime
interactive headroom.

The operator wants:

* Dispatch tight during operator-active hours (06:00-22:00 local),
* Dispatch aggressive overnight (22:00-06:00 local),
* End-of-week push (ADR-0006) confined to nighttime so daytime 5h
  windows stay free for interactive use,
* Daily/weekly transitions smooth, not whipsawed at the clock edge.

## Decision

Add a **time-of-day-modulated band layer** that sits on top of the
static throttle bands from ADR-0004. New settings under
`[throttle.time_of_day]` define the day window and ramp:

```toml
[throttle.time_of_day]
timezone     = ""        # IANA name; "" = system local
day_start    = "06:00"
day_end      = "22:00"
ramp_minutes = 30        # linear transition centered on each boundary
```

`[throttle.five_hour]` gains four optional override fields:

```toml
daytime_band_full_dispatch_max_pct   = 15
daytime_band_slowdown_max_pct        = 30
nighttime_band_full_dispatch_max_pct = 50
nighttime_band_slowdown_max_pct      = 75
```

Token-math anchor for the defaults: averaged across 16h day at 15%
and 8h night at 50%, the mean 5h utilization is ≈ 21.7%, which
yields ~95% weekly cap consumption over a 168h week — the operator's
target. Each override field is `int | None`; a `None` falls back to
the static `band_*` value so an operator who only tightens daytime
isn't forced to repeat the static value for night.

At each supervisor tick, `_effective_five_hour_thresholds` blends the
daytime and nighttime values linearly across a ramp **centered** on
each boundary (`day_start ± ramp_minutes/2`). The boundary itself is
exactly the midpoint of daytime/nighttime; the ramp ends are pure
daytime / nighttime respectively.

The EOW push from ADR-0006 gains a new gate:
`[throttle.weekly].eow_push_nighttime_only = true` (default). When set,
the PAUSED_WEEKLY → END_OF_WEEK_PUSH transition fires only during
**core** nighttime — the morning and evening ramps both fail the gate.
This guarantees the burn-down doesn't start just as the operator's
morning use begins. The 24h EOW window (widened from 12h in the same
PR) gives the gate multiple opportunities per week to fire.

## Alternatives considered

* **Single multiplier scaling all bands.** Two-knob solution: one
  number for day, one for night. Rejected because it couples
  `full_dispatch_max_pct` and `slowdown_max_pct` movement: an operator
  who wants tighter daytime full-dispatch but the same slowdown shape
  can't express it. The 4-field design keeps every cutoff independently
  tunable per ADR-0014.
* **Daytime-only hard cap (e.g. 25% of 5h budget regardless of band).**
  Rejected because it produces a discontinuity at the day/night
  boundary — dispatch jumps from full to halted. The ramp smooths
  this out without dropping a knob.
* **Per-hour bands.** Maximum flexibility but 24× the config burden;
  rejected as premature.

## Consequences

* (+) Operator's daytime interactive headroom is preserved by default
  (15% / 30% bands ≈ 85% headroom).
* (+) Nighttime push burns through the weekly cap before reset
  without the operator noticing.
* (+) Pre-modulation behavior remains expressible: set all override
  fields to the static band values (or leave them as `None`) and the
  effective bands equal the static bands.
* (-) Adds a timezone dependency: the supervisor now reads
  `[throttle.time_of_day].timezone` (defaulting to system local).
  Misconfigured timezones can produce surprising bands. Mitigated by
  ADR-0009's Clock protocol (timezone conversion is a single point of
  failure that's exercised by integration tests).
* (-) Adds two new pure modules (`supervisor.time_of_day`,
  `supervisor.pacing`) to maintain. Both are tiny (≤50 LOC each) and
  100% test-covered.

## Reversibility

High. To disable time-of-day modulation entirely:

* Leave all `daytime_*` / `nighttime_*` override fields as `None`
  (default in the schema; the TOML defaults set them but a per-queue
  override can null them).
* Set `[throttle.weekly].eow_push_nighttime_only = false` to restore
  the ADR-0006 EOW push behavior.

Amendments: this ADR amends ADR-0004 (introduces an optional layer on
top of the static bands) and ADR-0006 (adds the nighttime gate to the
EOW push transition). Neither prior ADR is superseded — their decisions
remain authoritative when the time-of-day layer is disabled.
