# Architecture

This is a living document. Any PR that changes a cross-component contract,
adds or removes a top-level module under `src/claude_task_runner/`, or
introduces a new on-disk file MUST update this document in the same PR.

## Component map

```
+--------------------------------------------------------------------+
|                        CLI (cli/)                                  |
|   typer-based entry; dispatches to subcommands per concern         |
+--+------+----------+-----------+---------+--------+----------+-----+
   |      |          |           |         |        |          |
   v      v          v           v         v        v          v
+-----+ +------+ +--------+ +--------+ +-------+ +-------+ +--------+
|usage| |queue | |runner  | |sup-    | |cron / | |doctor | |worktree|
|     | |      | |        | |ervisor | |systemd| |       | |reclaim |
+-----+ +------+ +--------+ +--------+ +-------+ +-------+ +--------+
   |      |          |           |                             |
   v      v          v           v                             v
+---------------------+ +-------------------+         +---------------+
| YAML state          | | UsageSource       |         | git: per-task |
| (queue/store)       | | (usage/source)    |         | worktrees     |
+---------------------+ +-------------------+         +---------------+
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
   transitions additionally surface as `EmitEvent` actions
   (`state_transition`, `drift_detected`, `usage_capture_error`, ...).
   `supervisor start` wires no `event_callback`, so each event is logged
   to the supervisor log at DEBUG level only, below the default
   `[logging].level` of INFO; no `events.ndjson` file is written today.
   See [Supervisor log and drift evidence](#supervisor-log-and-drift-evidence).
5. `runner.heartbeat` watches the last event timestamp; marks task `possibly_hung`
   after `task_caps.heartbeat_silence_alert_s` seconds of silence.
6. If task hits a sidecar question: writes
   `<queue>/.claude_task_runner/sidecar/<id>/request-NNN.json`, transitions
   task to `awaiting_sidecar`. The request stays open until **every**
   question id it asked appears in the response's `answers` (ADR-0031) — a
   response file alone does not close it. Operator answers via
   `/runner-answer-sidecar`,
   which writes `response-NNN.json`. Supervisor re-dispatches via `claude --resume
   <session_id>`.
7. On 5h-window reset mid-task: in-flight task continues. Supervisor's
   `runner.session.plan_next_spawn` knows that resuming a task across a window
   boundary is fine because we use `--resume <session_id>`.
8. On task failure: `runner.retry` classifies the error
   (environmental | operator | task | unknown). Environmental → auto-retry.
   Other → surface to operator.
10. After the task's branch has been merged into the parent branch (e.g. by
    the `runner-merge-claude-branches` consolidation), `worktree.reclaim`
    removes the task's worktree and local branch (ADR-0034). It runs from
    `claude-task-runner worktree reclaim` on demand, or from the supervisor
    loop every `[worktree_reclaim].interval_s` when
    `[worktree_reclaim].periodic` is on. The runner still never *creates* a
    worktree; that stays the pre-dispatch hook's job (ADR-0013).

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
    ├── state/.corrupt/<id>.<ts>.yaml  # unparseable state YAMLs, quarantined
    │                                  #   (ADR-0028)
    ├── sidecar/<id>/request-NNN.json
    ├── sidecar/<id>/response-NNN.json
    ├── force_dispatch/<id>.req     # `queue force-dispatch` requests, consumed
    │                               #   on the next supervisor tick
    ├── logs/<id>/                  # per-attempt worker output (ADR-0025):
    │   ├── attempt-<N>.stream.jsonl  #   parsed stdout NDJSON stream (re-read
    │   │                             #   on adoption to rebuild StreamSummary)
    │   └── attempt-<N>.stderr        #   paired stderr (error tail kept in state)
    ├── supervisor.json             # supervisor state machine snapshot; holds
    │                               #   last_drift_message while in ErrorDrift
    ├── supervisor.pid              # PID of the running supervisor
    ├── supervisor.log              # supervisor stdout/stderr, only when the
    │                               #   cron watchdog started it (see below)
    └── usage_captures/<ts>.cap     # raw PTY captures of the supervisor's
                                    #   /usage polls (rotated)
```

Global (cross-queue):

```
~/.claude_task_runner/
├── global.lock                     # fcntl lock; single supervisor across queues
├── queues.json                     # queues the cron watchdog manages
│                                   #   (`watchdog register`)
├── watchdog_state.json             # cron watchdog restart history + backoff
├── watchdog.log                    # cron watchdog output (watchdog.sh)
├── usage_captures/<ts>.cap         # raw PTY captures from the `usage` CLI
│                                   #   commands (rotated)
└── crontab.backup.<ts>             # crontab snapshot before install
```

### Supervisor log and drift evidence

The supervisor logs to stderr, so where its log lands depends on what
started it:

| Started by | Supervisor log |
|---|---|
| systemd user unit (what `claude-task-runner install` sets up by default when `systemctl --user` works) | journald: `journalctl --user -u claude-task-runner` |
| cron watchdog (`watchdog.sh` → `claude-task-runner watchdog tick`) | `<queue>/.claude_task_runner/supervisor.log` |
| `claude-task-runner supervisor start` run by hand | that terminal |

At the default `[logging].level` of INFO, `Notify` actions appear as
`notify[<level>]: <message>` lines on entry to `ErrorDrift`, `SlowingDown`,
`Throttled5h` and `ThrottledWeekly`. `EmitEvent` actions (`state_transition`,
`drift_detected`, `drift_clean_poll`, `usage_capture_error`, ...) are logged
at DEBUG level only; set `[logging].level = "DEBUG"` in `claude_runner.toml`
to see them. A usage capture that times out or cannot spawn `claude` produces
only a `usage_capture_error` event, so at INFO it leaves no trace in the log.

There is no separate drift log. Parser drift leaves three pieces of evidence:

- `claude-task-runner supervisor status` shows state `error_drift` and a
  `Last drift:` line, read from `last_drift_message` in `supervisor.json`.
- The supervisor log has one `notify[error]: parser drift: ...` line from the
  tick that entered `ErrorDrift`.
- With the TTY usage source, the capture that failed to parse is the newest
  `<queue>/.claude_task_runner/usage_captures/<ts>.cap`. The API source writes
  no `.cap`, and neither does a capture that times out or cannot spawn.

## Key invariants

These properties are never violated; tests and assertions enforce them.

1. **At most one supervisor process per host** — enforced by `fcntl.flock` on
   `~/.claude_task_runner/global.lock`.
2. **In-flight tasks are never killed by supervisor death** — supervisor
   shutdown writes state and exits; tasks continue. Supervisor restart reattaches
   to live PIDs.
3. *(Retired 2026-09-25.)* This slot said a utilization decrease without a
   detected reset is `UsageFormatDrift`. Nothing ever enforced that: the check
   was written but never called, and it has been removed. Each reading is used
   as reported. The slot keeps its number because code comments cite these
   invariants by number.
4. **No new dispatch when 5h utilization ≥ the active band's stop threshold**
   (`fivehr_stop_pct` in `[dispatch_pct.day]` or `[dispatch_pct.night]`) —
   this is the safety net.
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
9. **A task worktree is removed only when nothing in it can be lost** — the
   task is `completed` and not in flight, its branch is an ancestor of
   `<remote>/<parent_branch>` right after a fetch, and `git status` is clean
   apart from allow-listed untracked paths. The branch goes with
   `git branch -d`, never `-D`. See ADR-0034.

## Extension points

Operators extend behavior without code changes:

- **Failure patterns**: edit `[failure_classifier]` in `claude_runner.toml`.
- **Effort levels**: edit `[effort_levels]` in `claude_runner.toml`.
- **Pre/post-dispatch hooks**: set `[hooks].pre_dispatch_command` and
  `post_dispatch_command`.
- **Worktree reclamation**: `[worktree_reclaim]` sets the branch template,
  parent branch, disposable untracked paths, the hook's lock file, and the
  opt-in periodic supervisor pass (ADR-0034).
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
