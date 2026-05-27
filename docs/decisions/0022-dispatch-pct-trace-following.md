# ADR-0022: `[dispatch_pct.*]` variant-C trace-following, replacing `[throttle.*]`

- **Date:** 2026-05-27
- **Status:** accepted
- **Supersedes:** ADR-0015, ADR-0016

## Context

The throttle subsystem accreted three layers over the course of
ADR-0004 → ADR-0015 → ADR-0016:

1. **Static bands** (`band_full_dispatch_max_pct`,
   `band_slowdown_max_pct`, `pause_at_pct`) as a safety floor.
2. **Time-of-day overlays** with four day/night overrides per band
   and a `ramp_minutes` linear smoother around each boundary.
3. **Dynamic weekly pacing curve** with a piecewise-linear target,
   `pacing_slack_pp` dead-band, and an out-of-slack band shift —
   the "slack-and-shift" model — plus a separate `PAUSED_WEEKLY`
   hard-floor state and `END_OF_WEEK_PUSH` overlay state with its
   own nighttime gate.

To predict supervisor behaviour the operator now has to reason about
all three layers and the interactions between them. In practice the
static bands are shadowed by the dynamic curve in steady-state
operation, `pacing_slack_pp` whipsaws bands near target, and the
`PAUSED_WEEKLY → END_OF_WEEK_PUSH` transition's `eow_push_nighttime_only`
gate has become a fourth condition layered on the curve.

The operator's mental model is simpler than what the schema reflects:

- During the day, leave most of the 5h budget for interactive use;
  at night, push harder. (one step function)
- Over a week, spend ~linearly until the last day or two, then push
  toward the cap. (one sloped line)

This ADR collapses the schema and the math to match that mental
model.

## Decision

Replace the `[throttle.*]` configuration tree and its three-layer
implementation with a single `[dispatch_pct.*]` tree implementing
**variant-C trace-following**:

### Schema

```toml
[dispatch_pct]
timezone = ""             # IANA name; "" = system local

[dispatch_pct.day]
fivehr_slowdown_pct = 40  # 5h util where SLOWING_DOWN begins
fivehr_stop_pct     = 60  # 5h util where THROTTLED_5H begins

[dispatch_pct.night]
fivehr_slowdown_pct = 70
fivehr_stop_pct     = 90
time_start          = "21:00"
time_end            = "06:00"

[dispatch_pct.week]
early_pct       = 60      # target % at the start of the EOW segment
eow_pct         = 95      # target % at week reset
eow_time_switch = "40h"   # duration; sets the curve breakpoint
```

Per-account `<config_dir>/runner-account.toml` uses the same keys
with `T | None` field types — `None` inherits the queue-wide value.

### Decision rule

Per supervisor tick:

1. **Weekly first.** Compute `target_pct(elapsed_now)` on a piecewise-
   linear curve anchored to `UsageReading.seven_day.resets_at`. If
   `observed > target`: `THROTTLED_WEEKLY` (no dispatch). Else weekly
   allows dispatch.
2. **Then 5h.** Pick `day` or `night` band by local time-of-day at
   a hard step on each boundary. Compare 5h utilization to
   `fivehr_slowdown_pct` / `fivehr_stop_pct`:
   - `obs5h ≥ stop_pct`: `THROTTLED_5H`.
   - `obs5h ≥ slowdown_pct`: `SLOWING_DOWN` with linear concurrency
     ramp from `max_concurrency` (at `slowdown_pct`) to `0`
     (at `stop_pct`).
   - Else: `DISPATCHING` at `max_concurrency`.

The weekly side is binary (no slowdown band). All graceful
degradation lives on the 5h side. The operator's mental model is
satisfied with two pictures: a day/night step-function and a
sloped target line.

### Curve shape

Piecewise linear with one elbow:

```
target_pct
 ▲
 │                                                       eow_pct
 │                                                    ╭───●
 │                                              ╭─────╯
 │                                        ╭─────╯
 │                              early_pct ╭
 │                                  ╭─────╯
 │                            ╭─────╯
 │                      ╭─────╯
 │                ╭─────╯
 │          ╭─────╯
 │    ╭─────╯
 ╰────╯───────────────────────────┼───────────────────────●
 0                          1 - eow_window_fraction      1.0   elapsed
                            (= eow_time_switch / 7d)
```

`f = eow_time_switch_s / 7d`, breakpoint `B = 1 − f`. Curve segments:

- `0 ≤ t ≤ B`: `target = (t/B) · early_pct`
- `B < t ≤ 1`: `target = early_pct + ((t−B)/f) · (eow_pct − early_pct)`

### Analytical wakeup when THROTTLED_WEEKLY

When `observed > target(t)`, the curve is monotonically increasing
in `t` (assuming `early_pct < eow_pct`), so the supervisor solves
`target(t') = observed` and wakes at:

```
wakeup_at = max(now + poll_interval_s,
                min(next_5h_reset_wakeup,
                    resets_at − (1 − t') · 7d))
```

The outer `max` prevents busy-spin if `t' < t_now` (the operator was
throttled across a long sleep and the curve has already caught up).
The inner `min` keeps wakeup horizons readable — never sleep past
the next 5h reset.

### State machine

Drop `PAUSED_WEEKLY` and `END_OF_WEEK_PUSH` from `SupervisorState`.
Keep `IDLE / DISPATCHING / SLOWING_DOWN / THROTTLED_5H /
THROTTLED_WEEKLY / STOPPED / ERROR_DRIFT`. `PAUSED_WEEKLY`'s role
collapses into `THROTTLED_WEEKLY`; `END_OF_WEEK_PUSH`'s role is
subsumed by the natural rise of the trace from `early_pct` to
`eow_pct` over the EOW segment.

### Persistence migration

Bump `SUPERVISOR_SCHEMA_VERSION` from 3 to 4. The v3→v4 migration
in `supervisor.persistence.load` rewrites any persisted
`state="paused_weekly" | "end_of_week_push"` to `"idle"` (both at
the top level and inside every `accounts[*]` entry) and clears
`scheduled_wakeup_at` so the next tick recomputes against the new
math. In-flight tasks are preserved verbatim — the architectural
invariant that state transitions never kill in-flight work applies
to schema migrations too.

### Token budgets

Token budgets are derived from `[claude].plan` only. There is no
per-queue or per-account override of `five_hour_tokens` /
`weekly_tokens`. `[plans.*]` blocks simplify to token counts; the
previous `band_full_dispatch_max_pct` / `band_slowdown_max_pct`
plan-derived defaults are gone (those values now live entirely in
`[dispatch_pct.*]`).

### Loader behaviour

Any `[throttle.*]` block in a loaded TOML (queue-side or per-
account) raises `ConfigError` with a migration-hint message
naming the offending section. ADR-0007 commits this codebase to a
fresh-schema policy; silently dropping `[throttle.*]` would also
drop the safety-floor settings inside it, which has unacceptable
blast radius.

### Package layout

A new `claude_task_runner.throttle` package owns the dispatch math
end to end:

```
throttle/
  curve.py        — target_pct, elapsed_for_target_pct (pure math)
  time_of_day.py  — which_band (wrap-aware day/night step)
  policy.py       — resolve(queue, account) → ResolvedPolicy
  decision.py     — decide(policy, reading, clock) → Decision
```

`supervisor.state_machine.step()` retains its signature and becomes
a thin translator from `Decision` to `(snapshot, actions)`. The
former modules `supervisor.pacing`, `supervisor.throttle_merge`,
`supervisor.time_of_day` are deleted. The token-cost math in
`runner.concurrency` (`predict_post_dispatch_pct`,
`in_flight_estimate_tokens`) survives — it's orthogonal to
thresholds.

The name "throttle" survives as a module path because no shorter
single-word alternative captures the package's role. The word does
not appear in any field, identifier, or operator-facing string.

## Alternatives considered

- **Keep slack-and-shift; tighten defaults.** Rejected. The
  surprises operators reported (whipsaw at target boundary,
  static-band shadow, `pause_at_pct` floor contradicting curve
  intent) are layer-interaction problems, not parameter-tuning
  problems. No defaults change reduces the cognitive load of
  reasoning about three layers.
- **Variant A: weekly slowdown band at `target − N pp`.** Mirrors
  the 5h shape exactly. Rejected because the per-tick weekly
  target already moves with elapsed; a moving slowdown band that
  also moves with elapsed is harder for operators to reason about
  than a binary line.
- **Variant B: time-of-day weekly biasing.** Apply nighttime/daytime
  modulation to weekly target. Rejected because the operator's
  intuition is that nighttime *5h* aggression already accomplishes
  the same goal — and the weekly curve serves a different purpose
  (spread across the week) that doesn't benefit from a clock-time
  axis.
- **Keep `ramp_minutes` time-of-day smoothing.** Rejected. With
  `poll_interval_s ≈ 60s` and a 30-minute ramp, the smoothing
  window covers only ~30 ticks; the boundary crossing has been
  invisible in practice. A hard step is no worse and removes a knob.
- **Migrate `[throttle.*]` keys silently.** Rejected per ADR-0007.

## Consequences

- (+) Operator can model the supervisor's behaviour with two
  pictures (day/night step + sloped curve) instead of three layers.
- (+) Removes seven knobs: `band_full_dispatch_max_pct` /
  `band_slowdown_max_pct` (two windows × two = four), `pause_at_pct`,
  `eow_push_nighttime_only`, `pacing_slack_pp`, `pre_eow_target_pct`
  rename, `ramp_minutes`. Each removal eliminates a misconfiguration
  vector.
- (+) Analytical wakeup gives weekly THROTTLED a horizon closer to
  "when the supervisor will reclassify" rather than the previous
  "next 5h reset" placeholder — more useful in `runner-status`.
- (+) `ResolvedPolicy` is a single frozen dataclass with no
  `Optional` fields; downstream code reads it without per-tick merge
  logic.
- (−) Existing operator TOMLs do not parse. Doctor surfaces this
  loudly; the cheatsheet's migration table maps every old key.
- (−) The `SUPERVISOR_SCHEMA_VERSION` bump means a fresh-restart
  supervisor on a v3 snapshot performs a state rewrite to IDLE on
  first load; the next tick reclassifies. Acceptable for an
  operator-initiated upgrade.
- (−) Loss of the static safety floor on weekly (`pause_at_pct`
  hard cap). The trace itself caps at `eow_pct` at `t=1`, which
  serves the same purpose — observed > `eow_pct` is impossible on
  a clean curve. If the curve's `resets_at` is unparseable, the
  decision falls back to "allow dispatch"; this is the same
  behaviour as ADR-0016 had on the same edge.

## Reversibility

Medium. The schema change is hard-cutover (ADR-0007). Rolling back
to `[throttle.*]` would require:

- Restoring the deleted modules from git history.
- Reverting the `SUPERVISOR_SCHEMA_VERSION` bump (and writing a
  v4→v3 reverse migration that drops the new dispatch_pct fields).
- Rewriting operator TOMLs back to `[throttle.*]`.

The math itself is reversible — variant-C is more constrained than
slack-and-shift but every old curve shape is expressible as a
limiting case (`pacing_slack_pp = 0` ≈ trace-following with no
slack). The information lost is in the dropped static bands and
the time-of-day ramp, neither of which had observable production
impact.

## Notes

The three operator TOMLs that must be rewritten to match the new
schema are migrated outside this PR:

- `/home/bill/gitlab/nlmixr2lib_ingestion/claude_runner.toml`
- `~/.claude/runner-account.toml`
- `~/.claude_personal/runner-account.toml`

`claude-task-runner doctor` grows a `check_dispatch_pct_legacy`
that scans every resolved account's `runner-account.toml` for a
`[throttle.*]` block and FAILs with the migration message. The
operator's first post-merge `doctor` run is the migration checklist.
