# Cheat sheet

Quick reference for common operator tasks. Pairs with the longer
[runbook](runbook.md) and the [architecture overview](architecture.md);
links to the decision log under [decisions/](decisions/) explain the
"why" behind each knob.

## Current state at a glance

| Want to see | Command |
|---|---|
| Supervisor state + utilization | `claude-task-runner supervisor status` |
| Live 5h / weekly utilization | `claude-task-runner usage` (parses `claude /usage`) |
| Full machine-readable snapshot (state, util, next wakeup) | `claude-task-runner supervisor status --json` |
| Pending sidecar questions | `/runner-answer-sidecar` skill or `claude-task-runner sidecar list` |
| Recent supervisor transitions | `tail -F <queue>/.claude_task_runner/supervisor.log` |
| Drift / capture failures | `tail -F <queue>/.claude_task_runner/drift.log` |

## Configuration precedence

For every setting in `[dispatch_pct.*]` (and elsewhere):

1. **Per-queue** `<queue>/claude_runner.toml` wins.
2. **Package defaults** at `src/claude_task_runner/config/defaults/settings.toml`
   are merged underneath (deep merge — nested tables compose, scalars are
   overwritten).
3. **Schema defaults** in `src/claude_task_runner/config/schema.py` apply
   when a field is absent from both above.

So an existing per-queue TOML keeps working when new fields land — the
schema's default fills in.

## Tuning dispatch_pct

ADR-0022 (variant-C trace-following) collapsed the previous three-layer
throttle stack into two pictures the operator can hold in mind:

1. **5h side** — a day/night step function. Below `fivehr_slowdown_pct`
   dispatch is full; between slowdown and stop a linear concurrency ramp
   tapers to zero; at or above `fivehr_stop_pct` dispatch halts. Day vs.
   night is a hard step at the configured boundary. The night window
   may wrap midnight (`time_start > time_end`, e.g. 21:00→06:00) or be
   same-day (`time_start < time_end`, e.g. 01:00→10:00).
2. **Weekly side** — a single sloped target line. From 0 at week start
   rising to `early_pct` at the EOW elbow (`eow_time_switch` before
   week reset), then steeper to `eow_pct` at reset. If observed weekly
   utilization is above the target line, dispatch is THROTTLED_WEEKLY
   until the line catches up. No weekly slowdown band — all graceful
   degradation lives on the 5h side.

### 5h side — day / night bands

| Setting | Default | When to bump | When to lower |
|---|---|---|---|
| `[dispatch_pct.day].fivehr_slowdown_pct` | 40 | Push later for queues with bursty short tasks that recover fast | Tighten for shared accounts with heavy interactive use |
| `[dispatch_pct.day].fivehr_stop_pct` | 60 | Toward 70 if you want a wider slowdown ramp | Toward 50 to halt earlier (more conservative) |
| `[dispatch_pct.night].fivehr_slowdown_pct` | 70 | Toward 80 to push harder at night | Toward 60 if night usage competes with backup jobs |
| `[dispatch_pct.night].fivehr_stop_pct` | 90 | Hardly ever — 90% leaves a 10pp safety margin | Toward 80 for extra headroom against an end-of-window burst |
| `[dispatch_pct.night].time_start` | `"21:00"` | Later if your interactive day extends later | Earlier to begin the nighttime burn sooner |
| `[dispatch_pct.night].time_end` | `"06:00"` | Later if you're a late riser | Earlier if you start work early |
| `[dispatch_pct.timezone]` | `""` (system local) | Set explicitly when running in a container with `TZ` unset, or when shared across accounts in different zones | n/a |

### Weekly side — trace target curve

| Setting | Default | When to bump | When to lower |
|---|---|---|---|
| `[dispatch_pct.week].early_pct` | 60 | Toward 70 if you want less reserved for the EOW burst | Toward 50 for a bigger end-of-week push |
| `[dispatch_pct.week].eow_pct` | 95 | Toward 99 if your queue can reliably burn the final tokens; risky | Toward 90 for more safety margin |
| `[dispatch_pct.week].eow_time_switch` | `"40h"` | Lengthen if your nighttime windows are narrow and you want multiple opportunities | Shorten to confine the steep ramp closer to reset |

### How the decision composes (per tick)

1. **Weekly first.** Compute `target_pct(elapsed_now)` on the
   piecewise-linear curve. If `observed_weekly > target` →
   `THROTTLED_WEEKLY`, target_concurrency=0, wakeup analytically
   computed (when the curve rises to meet observed), clamped to the
   next 5h reset.
2. **Then 5h.** Pick day or night band by local time-of-day.
   Compare observed 5h utilization to the band's thresholds and
   classify as `DISPATCHING` / `SLOWING_DOWN` / `THROTTLED_5H`.
3. The state machine emits Notify + EmitEvent on transitions; the
   payload of `throttled_weekly_entry` carries `observed_pct` and
   `target_pct` so the operator can audit the trace-following math.

## Common operator tasks

### Read the current supervisor state

```sh
claude-task-runner supervisor status --json
```

The JSON snapshot includes `state`, `last_5h_util_pct`,
`last_weekly_util_pct`, and `scheduled_wakeup_at` — the inputs and
output of the most recent dispatch decision.

On each transition the supervisor also emits `state_transition` /
`throttled_5h_entry` / `throttled_weekly_entry` events whose payloads
carry the band, thresholds, and `target_pct`. By default these are
written to the supervisor log
(`<queue>/.claude_task_runner/supervisor.log`); there is no separate
`events.ndjson` sink unless a host wires an event callback.

### Make a queue push harder during the day

```toml
# <queue>/claude_runner.toml
[dispatch_pct.day]
fivehr_slowdown_pct = 60
fivehr_stop_pct     = 80
```

### Stretch the EOW push window

```toml
[dispatch_pct.week]
eow_time_switch = "48h"   # was "40h" — extra night of opportunity
```

<a id="migration-from-throttle"></a>
### Migration from [throttle.*] (ADR-0022)

Old → new mapping for the `[throttle.*]` blocks that ADR-0022 retired.
The loader hard-errors on any lingering `[throttle.*]` key — the
mapping table below is the migration recipe.

| Old (`[throttle.*]`) | New (`[dispatch_pct.*]`) |
|---|---|
| `[throttle.five_hour].daytime_band_full_dispatch_max_pct` | `[dispatch_pct.day].fivehr_slowdown_pct` |
| `[throttle.five_hour].daytime_band_slowdown_max_pct` | `[dispatch_pct.day].fivehr_stop_pct` |
| `[throttle.five_hour].nighttime_band_full_dispatch_max_pct` | `[dispatch_pct.night].fivehr_slowdown_pct` |
| `[throttle.five_hour].nighttime_band_slowdown_max_pct` | `[dispatch_pct.night].fivehr_stop_pct` |
| `[throttle.time_of_day].day_end = "21:00"` | `[dispatch_pct.night].time_start = "21:00"` |
| (no equivalent) | `[dispatch_pct.night].time_end = "06:00"` (set explicitly) |
| `[throttle.weekly].pre_eow_target_pct` | `[dispatch_pct.week].early_pct` |
| `[throttle.weekly].eow_target_pct` | `[dispatch_pct.week].eow_pct` |
| `[throttle.weekly].eow_window_s = 144000` | `[dispatch_pct.week].eow_time_switch = "40h"` |
| `[throttle.five_hour].band_*` static safety floor | DROPPED — weekly trace and 5h day/night cover the same role |
| `[throttle.weekly].band_*` / `pause_at_pct` | DROPPED — `eow_pct` is the implicit cap |
| `[throttle.weekly].pacing_curve_enabled` | DROPPED — curve is always on |
| `[throttle.weekly].pre_eow_target_pct` / `pacing_slack_pp` | DROPPED slack; renamed to `early_pct` (always-on, no slack) |
| `[throttle.weekly].eow_push_nighttime_only` | DROPPED — night dispatch_pct biases nighttime via its own thresholds |
| `[throttle.time_of_day].ramp_minutes` | DROPPED — hard step at the boundary |

Run `claude-task-runner doctor` to list any TOML still carrying a
legacy block.

### Stale branch cleanup

```sh
# List merged-and-deletable local branches
git branch --merged main

# Delete a merged local branch
git branch -d <branch>

# Force-delete (uncommitted work will be lost — confirm first)
git branch -D <branch>

# Delete a remote branch (uses git auth, not gh API key — works even with read-only gh)
git push origin --delete <branch>

# Prune stale remote-tracking refs after PRs auto-delete their branches on merge
git remote prune origin
```

## Raising the coverage gate

The CI gate is currently 75% (`--cov-fail-under=75` in
`.github/workflows/ci.yml`); the aspirational target is 90%. To find
where the current gaps are, run the suite with a per-module miss report
and sort by what's least covered:

```sh
pytest -m "not live" --cov --cov-report=term-missing
```

The gaps cluster in two predictable places: the typer CLI command
modules (`cli/*_cmd.py`), which need CLI-invocation harnesses, and the
I/O-heavy `runner/orchestrator`, `supervisor/daemon`, and
`usage/capture` modules (the last covered only by the live-test suite,
`CTR_RUN_LIVE_TESTS=1`, because it requires a real `claude` binary). The
pure logic in `throttle/` (`curve`, `time_of_day`, `policy`, `decision`)
and `supervisor/state_machine` carries the project's highest coverage.

## Add a new plan

Plans live under `[plans.*]` in the package defaults TOML. Each entry
declares `five_hour_tokens` and `weekly_tokens`. Anthropic announces
a new tier, you want to calibrate against it:

1. Authenticate against the new account: `claude --config-dir
   ~/.claude_<tier>` then `claude /login`.
2. Capture a few real `/usage` readings via the supervisor's
   `claude-task-runner usage` command to discover the actual budget
   ceiling.
3. Add an entry to `[plans.*]` in the per-queue or package TOML:

   ```toml
   [plans.<tier>]
   five_hour_tokens = <observed 5h cap>
   weekly_tokens    = <observed weekly cap>

   [claude]
   plan       = "<tier>"
   config_dir = "~/.claude_<tier>"
   ```

4. Restart the supervisor: `claude-task-runner supervisor restart`.

## Where each setting lives in the schema

For type-checking your TOML overrides locally:

```python
from claude_task_runner.config.schema import Settings
from claude_task_runner.config.loader import load_settings
print(load_settings(Path("path/to/queue/claude_runner.toml")))
```

A schema mismatch (typo, wrong type, value out of range) is caught at
`load_settings` time with a Pydantic error pointing at the offending
field.

## Further reading

* [Runbook](runbook.md) — long-form operator procedures (start/stop,
  drift recovery, sidecar workflow, lifecycle of a task).
* [Architecture](architecture.md) — module layout, state machine,
  on-disk file map, invariants.
* [Decision log](decisions/) — ADRs for every cross-component decision.
  Key ones for dispatch_pct:
  * [ADR-0022](decisions/0022-dispatch-pct-trace-following.md) — current dispatch policy (variant-C trace-following).
  * [ADR-0009](decisions/0009-clock-protocol-for-testability.md) — Clock protocol contract.
  * [ADR-0014](decisions/0014-all-cutoffs-as-settings.md) — every cutoff is a TOML setting.
  * [ADR-0004](decisions/0004-three-band-throttle-70-90.md) — *historical*: original three-band model.
  * [ADR-0006](decisions/0006-weekly-pause-with-eow-push.md) — *historical*: weekly pause and EOW push.
  * [ADR-0015](decisions/0015-time-of-day-band-modulation.md) — *superseded by ADR-0022*.
  * [ADR-0016](decisions/0016-dynamic-weekly-pacing-curve.md) — *superseded by ADR-0022*.
