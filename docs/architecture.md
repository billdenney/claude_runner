# Architecture

This is a living document. Any PR that changes a cross-component contract,
adds or removes a top-level module under `src/claude_task_runner/`, or
introduces a new on-disk file MUST update this document in the same PR.

## Component map

```
+----------------------------------------------------------------+
|                        CLI (cli/)                              |
|   typer-based entry; dispatches to subcommands per concern     |
+--+------+----------+-----------+---------+--------+------------+
   |      |          |           |         |        |
   v      v          v           v         v        v
+-----+ +------+ +--------+ +--------+ +-------+ +-------+
|usage| |queue | |runner  | |sup-    | |cron / | |doctor |
|     | |      | |        | |ervisor | |systemd| |       |
+-----+ +------+ +--------+ +--------+ +-------+ +-------+
   |      |          |           |
   |      |          v           |
   |      |      +------+        |
   |      |      | EMA  |        |
   |      |      +------+        |
   |      |                      |
   v      v                      v
+----------------+      +-------------------+
| YAML state     |      | UsageSource       |
| (queue/store)  |      | (usage/source)    |
+----------------+      +-------------------+
                                 |
                                 v
                         +-------------+
                         | parser+drift|
                         |  + capture  |
                         +-------------+
                                 |
                                 v
                          claude /usage
```

## Data flow: lifecycle of one task

1. Operator adds a task: `claude-task-runner queue add` (or `/runner-add-task`).
   Task YAML lands in `<queue>/todo/<id>.yaml`.
2. Supervisor poll tick: reads usage, asks the throttle decision
   (`throttle.decision.decide`, via `supervisor.state_machine.step`) whether to dispatch.
3. If dispatch is approved: `runner.dispatcher` spawns `claude --print
   --output-format=stream-json --verbose ...`. Captures `session_id` from the
   first stream-json `system/init` event.
4. `runner.stream` consumes NDJSON line-by-line, updating
   `<queue>/.claude_task_runner/state/<id>.yaml`. Supervisor state-machine
   transitions additionally surface as `EmitEvent` actions, but these are
   logged (default `event_callback` is `None`, routing them to the
   supervisor log); no `events.ndjson` file is written today.
5. `runner.heartbeat` watches the last event timestamp; marks task `possibly_hung`
   after `task_caps.heartbeat_silence_alert_s` seconds of silence.
6. On task completion: `runner.ema` updates per-(model, effort, tool-hash) EMA
   with observed token / duration / cost samples.
7. If task hits a sidecar question: writes
   `<queue>/.claude_task_runner/sidecar/<id>/request-NNN.json`, transitions
   task to `awaiting_sidecar`. Operator answers via `/runner-answer-sidecar`,
   which writes `response-NNN.json`. Supervisor re-dispatches via `claude --resume
   <session_id>`.
8. On 5h-window reset mid-task: in-flight task continues. Supervisor's
   `runner.session.plan_next_spawn` knows that resuming a task across a window
   boundary is fine because we use `--resume <session_id>`.
9. On task failure: `runner.retry` classifies the error
   (environmental | operator | task | unknown). Environmental → auto-retry.
   Other → surface to operator.

## State machine summary

States in `supervisor/states.py`:

- `Idle` — no pending tasks; polling only.
- `Dispatching` — predicted 5h pct < `dispatch_pct.<band>.fivehr_slowdown_pct`.
- `SlowingDown` — predicted 5h pct in [slowdown, stop); target concurrency reduced linearly.
- `Throttled5h` — 5h utilization ≥ `fivehr_stop_pct` for the active band.
- `ThrottledWeekly` — observed weekly utilization > `target_pct(elapsed_now)` on the trace curve.
- `ErrorDrift` — last poll raised `UsageFormatDrift`; requires N clean polls to recover.
- `Stopped` — operator-issued stop.

The state machine itself (`supervisor/state_machine.py`) is a thin
wrapper that translates the result of `throttle.decision.decide()`
into `(snapshot, actions)`. Both `decide()` and `step()` are pure;
all I/O happens in `supervisor/daemon.py` based on the action list.

### Per-tick decision (ADR-0022, variant-C)

`decide()` walks the inputs in a fixed order:

1. **Weekly first.** `throttle.curve.target_pct(elapsed_now, …)`
   evaluates the piecewise-linear curve anchored to
   `reading.seven_day.resets_at`. If `observed > target`,
   `ThrottledWeekly` with `target_concurrency=0` and an analytical
   wakeup (`elapsed_for_target_pct(observed)` mapped back to a
   datetime, clamped to the next 5h reset and `now + poll_interval_s`).
2. **Then 5h.** `throttle.time_of_day.which_band(now_local, …)`
   picks `day` or `night` (wrap-aware hard step). Compare observed
   5h utilization to that band's `fivehr_slowdown_pct` /
   `fivehr_stop_pct`; classify into `Dispatching`, `SlowingDown`,
   or `Throttled5h`. The linear concurrency ramp shape is unchanged
   from ADR-0004.

The math is centralised in the `throttle/` package (`curve.py`,
`time_of_day.py`, `policy.py`, `decision.py`). All pure functions;
all 100% test coverage in `tests/unit/test_curve.py`,
`test_dispatch_time_of_day.py`, `test_policy.py`, `test_decision.py`.

## On-disk layout (per queue)

```
<queue>/
├── claude_runner.toml              # per-queue config; overrides defaults
├── todo/                           # input: task YAMLs awaiting dispatch
│   └── <id>.yaml
└── .claude_task_runner/            # all runtime state lives here
    ├── state/<id>.yaml             # TaskState (pydantic v2 schema) — the
    │                               #   single source of truth per task; stream
    │                               #   events are folded into it, not teed out
    ├── sidecar/<id>/request-NNN.json
    ├── sidecar/<id>/response-NNN.json
    ├── logs/<id>/                  # per-attempt worker output (ADR-0025):
    │   ├── attempt-<N>.stream.jsonl  #   parsed stdout NDJSON stream (re-read
    │   │                             #   on adoption to rebuild StreamSummary)
    │   └── attempt-<N>.stderr        #   paired stderr (error tail kept in state)
    ├── supervisor.json             # supervisor state machine snapshot
    ├── supervisor.pid              # PID of the running supervisor
    ├── supervisor.log              # supervisor lifecycle + transitions
    ├── watchdog.log                # cron/systemd watchdog actions
    ├── drift.log                   # parser drift + healthcheck results
    ├── usage_captures/<ts>.cap     # raw PTY captures of /usage (rotated)
    ├── ema.json                    # per-task-type EMA values
    └── banner.txt                  # human-readable status banner
```

Global (cross-queue):

```
~/.claude_task_runner/
├── global.lock                     # fcntl lock; single supervisor across queues
└── crontab.backup.<ts>             # crontab snapshot before install
```

## Key invariants

These properties are never violated; tests and assertions enforce them.

1. **At most one supervisor process per host** — enforced by `fcntl.flock` on
   `~/.claude_task_runner/global.lock`.
2. **In-flight tasks are never killed by supervisor death** — supervisor
   shutdown writes state and exits; tasks continue. Supervisor restart reattaches
   to live PIDs.
3. **Usage utilization is monotonically non-decreasing within a window** —
   any decrease without a detected reset is `UsageFormatDrift`.
4. **No new dispatch when 5h utilization ≥ no-dispatch threshold** —
   regardless of EMA prediction; this is the safety net.
5. **Every behavior-affecting cutoff is a setting** — no magic numbers for
   thresholds/timeouts/caps in runtime code (cosmetic presentation constants
   such as log-truncation widths are exempt; see ADR-0014). The merged
   `claude_runner.toml` (per-queue overrides + package
   `config/defaults/settings.toml` + schema defaults) is the single source of
   truth; `claude-task-runner doctor` loads it through the schema and surfaces
   any invalid override.
6. **All on-disk data has a `schema_version` field** — schema evolution can be
   detected and migrations versioned.
7. **`UsageFormatDrift` halts dispatch** — supervisor enters `ErrorDrift`;
   requires `usage.drift_recovery_clean_polls` consecutive clean readings to recover.
8. **Atomic writes for state files** — every YAML/JSON state write is via
   tempfile + `os.replace` to prevent torn reads.

## Extension points

Operators extend behavior without code changes:

- **Failure patterns**: edit `[failure_classifier]` in `claude_runner.toml`.
- **Effort levels**: edit `[effort_levels]` in `claude_runner.toml`.
- **Pre/post-dispatch hooks**: set `[hooks].pre_dispatch_command` and
  `post_dispatch_command`.
- **Notification channels**: set `[notify].channels` to any subset of
  `["desktop", "file", "webhook", "email"]`.
- **EMA priors per (model, effort)**: edit `[ema.priors.<model>.<effort>]`.
- **Task templates**: drop Jinja2 templates into
  `~/.claude_task_runner/templates/` or per-queue `templates/`.

## Anti-patterns (do NOT do these)

- **Don't sum local JSONL token counts as ground truth for windows.** The
  windows are server-side aggregations; local sums diverge unpredictably. See
  ADR-0001 / ADR-0008.
- **Don't hard-code thresholds.** All cutoffs are TOML settings. See ADR-0014.
- **Don't kill in-flight tasks on supervisor SIGTERM.** They are independent
  processes; reattach on restart. See ADR-0002.
- **Don't bypass the drift detection** — silent format drift is the worst
  failure mode. If drift is detected, halt dispatch until human review.
- **Don't introduce new on-disk files without updating this doc and the
  schema versioning.**
