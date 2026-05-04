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

1. Operator adds a task: `claude-task-runner queue add` (or `/runner-add-task`,
   or `queue add-batch <manifest>`). Task YAML lands in `<queue>/todo/<id>.yaml`.
2. Supervisor poll tick: reads usage, asks `runner.concurrency` whether to dispatch.
3. If dispatch is approved: `runner.dispatcher` spawns `claude --print
   --output-format=stream-json --verbose ...`. Captures `session_id` from the
   first stream-json `system/init` event.
4. `runner.stream` consumes NDJSON line-by-line, emitting events to
   `<queue>/.claude_task_runner/events.ndjson` and updating
   `<queue>/.claude_task_runner/state/<id>.yaml`.
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
   `runner.session.resume_or_fresh` knows that resuming a task across a window
   boundary is fine because we use `--resume <session_id>`.
9. On task failure: `runner.retry` classifies the error
   (environmental | operator | task | unknown). Environmental → auto-retry.
   Other → surface to operator.

## State machine summary

States in `supervisor/states.py`:

- `Idle` — no pending tasks; polling only.
- `Dispatching` — predicted_pct < `band_full_dispatch_max_pct`.
- `SlowingDown` — predicted_pct in slowdown band; target concurrency reduced linearly.
- `Throttled5h` — 5h utilization ≥ no-dispatch threshold.
- `PausedWeekly` — weekly utilization ≥ pause threshold.
- `EndOfWeekPush` — weekly is paused but reset is imminent; dispatches only short tasks.
- `ErrorDrift` — last poll raised `UsageFormatDrift`; requires N clean polls to recover.
- `Stopped` — operator-issued stop.

The state machine itself (`supervisor/state_machine.py`) is a pure function:
`step(state, reading, clock) → (new_state, actions)`. All I/O happens in
`supervisor/daemon.py` based on the action list.

## On-disk layout (per queue)

```
<queue>/
├── claude_runner.toml              # per-queue config; overrides defaults
├── todo/                           # input: task YAMLs awaiting dispatch
│   └── <id>.yaml
└── .claude_task_runner/            # all runtime state lives here
    ├── state/<id>.yaml             # TaskState (pydantic v2 schema)
    ├── sidecar/<id>/request-NNN.json
    ├── sidecar/<id>/response-NNN.json
    ├── logs/<id>/attempt-N.{stdout,stderr,streamjson}
    ├── events.ndjson               # canonical event stream
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
5. **Every cutoff is a setting** — no magic numbers in runtime code.
   `claude-task-runner config show` is the single source of truth.
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
