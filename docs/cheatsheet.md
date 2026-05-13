# Cheat sheet

Quick reference for common operator tasks. Pairs with the longer
[runbook](runbook.md) and the [architecture overview](architecture.md);
links to the decision log under [decisions/](decisions/) explain the
"why" behind each knob.

## Current state at a glance

| Want to see | Command |
|---|---|
| Supervisor state + utilization | `claude-task-runner status` |
| Live 5h / weekly utilization | `claude-task-runner usage` (parses `claude /usage`) |
| Effective bands for *right now* | `claude-task-runner status --verbose` (logs the computed `_EffectiveBands` snapshot) |
| Pending sidecar questions | `/runner-answer-sidecar` skill or `claude-task-runner sidecar list` |
| Recent supervisor transitions | `tail -F <queue>/.claude_task_runner/supervisor.log` |
| Drift / capture failures | `tail -F <queue>/.claude_task_runner/drift.log` |

## Configuration precedence

For every setting in `[throttle.*]` (and elsewhere):

1. **Per-queue** `<queue>/claude_runner.toml` wins.
2. **Package defaults** at `src/claude_task_runner/config/defaults/settings.toml`
   are merged underneath (deep merge — nested tables compose, scalars are
   overwritten).
3. **Schema defaults** in `src/claude_task_runner/config/schema.py` apply
   when a field is absent from both above.

So an existing per-queue TOML keeps working when new fields land — the
schema's default fills in.

## Tuning the throttle

The throttle has three independent layers; you can disable any of them
per queue without touching the others.

### Layer 1 — Static bands (ADR-0004, ADR-0006)

The original three-band model. Always active. Hard safety floor.

| Setting | Default | When to bump | When to lower |
|---|---|---|---|
| `[throttle.five_hour].band_full_dispatch_max_pct` | 70 | Hardly ever — let TOD layer do the day/night work | Tighten the hard ceiling, e.g. for shared accounts |
| `[throttle.five_hour].band_slowdown_max_pct` | 90 | If you're getting THROTTLED_5H at the band edge with quick tasks finishing | Be more conservative against late-tick overshoots |
| `[throttle.weekly].band_full_dispatch_max_pct` | 70 | Same as above for weekly | Same |
| `[throttle.weekly].band_slowdown_max_pct` | 90 | Same | Same |
| `[throttle.weekly].pause_at_pct` | 90 | NEVER lower below the slowdown band; this is the hard pause floor and is the one cutoff the pacing curve cannot override | If you want a stricter safety margin |
| `[throttle.weekly].eow_target_pct` | 95 | Toward 99 if your queue can reliably burn the final tokens; risky | Toward 90 for more safety margin |
| `[throttle.weekly].eow_window_s` | 86400 (24h) | If your nighttime is narrower than 8h, widening helps the gate fire | Cut back if the EOW push runs into the morning |
| `[throttle.weekly].eow_runtime_safety_factor` | 0.5 | If your tasks finish predictably and you trust the EMA | Lower for queues with high runtime variance |

### Layer 2 — Time-of-day modulation (ADR-0015)

Sits on top of Layer 1 for the 5h bands. Sleeping switch: leave all
four override fields as `None` (the schema default) and the layer is a
no-op.

| Setting | Default | When to bump | When to lower |
|---|---|---|---|
| `[throttle.time_of_day].timezone` | `""` (system local) | Set explicitly when running in a container with `TZ` unset, or when shared across accounts in different zones | n/a |
| `[throttle.time_of_day].day_start` | `"06:00"` | Push later if you start work later | Push earlier if you start work earlier |
| `[throttle.time_of_day].day_end` | `"22:00"` | Push later if you work into the night | Push earlier to start the nighttime burn sooner |
| `[throttle.time_of_day].ramp_minutes` | 30 | If you see whipsaw at the boundary (rare at 60s poll) | Down to 0 for a hard step — no interpolation |
| `[throttle.five_hour].daytime_band_full_dispatch_max_pct` | 15 | Loosen if you don't use Claude interactively much | Tighten further to almost no daytime dispatch |
| `[throttle.five_hour].daytime_band_slowdown_max_pct` | 30 | Same | Same |
| `[throttle.five_hour].nighttime_band_full_dispatch_max_pct` | 50 | Push harder at night | Be more conservative |
| `[throttle.five_hour].nighttime_band_slowdown_max_pct` | 75 | Same | Same |
| `[throttle.weekly].eow_push_nighttime_only` | `true` | n/a — this is a bool | Set `false` to allow daytime EOW push (legacy ADR-0006 behavior) |

### Layer 3 — Dynamic weekly pacing curve (ADR-0016)

Modulates weekly band thresholds based on the observed-vs-target curve.
Disabled by `pacing_curve_enabled = false`.

| Setting | Default | When to bump | When to lower |
|---|---|---|---|
| `[throttle.weekly].pacing_curve_enabled` | `true` | n/a | `false` to fall back to ADR-0006 behavior exactly |
| `[throttle.weekly].pre_eow_target_pct` | 80 | Push closer to 90 if you want less reserved for the EOW burst | Lower if you want a bigger end-of-week burst |
| `[throttle.weekly].pacing_slack_pp` | 10 | Wider band → less reactive (less whipsaw, slower correction) | Narrower band → more reactive |

### How the layers compose (per tick)

1. The state machine computes the **effective 5h bands** for *now* by
   interpolating daytime/nighttime values via Layer 2. With Layer 2
   disabled, falls back to Layer 1 static values.
2. Computes the **effective weekly bands** by shifting Layer 1 static
   values per Layer 3. With Layer 3 disabled, returns Layer 1 verbatim.
3. Classifies the supervisor state against these effective bands. The
   hard `pause_at_pct` floor is read from Layer 1 directly — Layer 3
   cannot override it.

## Common operator tasks

### Read the current effective bands

```sh
claude-task-runner status --verbose | grep -E "five_hour|weekly|pacing"
```

The `state_transition` events in `events.ndjson` also include
`five_hour_util` and `weekly_util` at the moment of the transition.

### Disable time-of-day modulation per queue (revert to ADR-0004 bands)

```toml
# <queue>/claude_runner.toml
[throttle.five_hour]
daytime_band_full_dispatch_max_pct   = -1   # use null
daytime_band_slowdown_max_pct        = -1
nighttime_band_full_dispatch_max_pct = -1
nighttime_band_slowdown_max_pct      = -1
```

(TOML doesn't have a literal `null`; use a per-queue override that sets
each to the corresponding static `band_*` value — the modulated result
collapses to the static value.)

### Disable the pacing curve per queue (revert to ADR-0006)

```toml
[throttle.weekly]
pacing_curve_enabled = false
```

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

The CI gate is currently 75% (set in `.github/workflows/ci.yml`). Aspirational
target is 90%. Modules currently below the global threshold (sorted by
gap to 80%):

* `cli/install_cmd.py` — 12% — typer plumbing; need CLI integration tests
* `cli/usage_cmd.py` — 18% — same
* `cli/supervisor_cmd.py` — 24% — same
* `runner/orchestrator.py` — 16% — needs unit tests against fake queue dirs
* `supervisor/daemon.py` — 48% — needs harness tests around the tick loop
* `cli/sidecar_cmd.py` — 56% — same as other CLI commands
* `cli/queue_cmd.py` — 59% — same
* `doctor/checks.py` — 68% — needs subprocess-mocked tests
* `runner/dispatcher.py` — 77% — close; needs edge cases
* `usage/capture.py` — 14% — by design; requires real `claude` binary
* `usage/source.py` — 43% — covered by live-test suite (`CTR_RUN_LIVE_TESTS=1`)

`supervisor.time_of_day`, `supervisor.pacing`, and `supervisor.state_machine`
are at 100% / 100% / 98% respectively. The new code is fully covered;
the global gap is in modules untouched by this PR.

## Add a new plan

Plans are described in `[throttle.five_hour]` / `[throttle.weekly]`
`budget_tokens` per queue. Anthropic announces a new tier, you want to
calibrate against it:

1. Authenticate against the new account: `claude --config-dir
   ~/.claude_<tier>` then `claude /login`.
2. Capture a few real `/usage` readings via the supervisor's
   `claude-task-runner usage` command to discover the actual budget
   ceiling (the 5h and weekly token counts the plan advertises).
3. Create the per-queue config:

   ```toml
   # <queue>/claude_runner.toml
   [claude]
   config_dir = "~/.claude_<tier>"

   [throttle.five_hour]
   budget_tokens = <observed 5h cap>

   [throttle.weekly]
   budget_tokens = <observed weekly cap>
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
  Key ones for throttling:
  * [ADR-0004](decisions/0004-three-band-throttle-70-90.md) — static three-band model.
  * [ADR-0006](decisions/0006-weekly-pause-with-eow-push.md) — weekly pause and EOW push.
  * [ADR-0009](decisions/0009-clock-protocol-for-testability.md) — Clock protocol contract.
  * [ADR-0014](decisions/0014-all-cutoffs-as-settings.md) — every cutoff is a TOML setting.
  * [ADR-0015](decisions/0015-time-of-day-band-modulation.md) — time-of-day modulation.
  * [ADR-0016](decisions/0016-dynamic-weekly-pacing-curve.md) — dynamic weekly pacing.
