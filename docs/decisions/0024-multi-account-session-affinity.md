# ADR-0024: Multi-account session affinity

- **Date:** 2026-05-29
- **Status:** proposed
- **Related:** ADR-0005 (resume with fresh fallback), ADR-0012
  (extensible failure classifier), ADR-0021 (long-lived OAuth token
  per account), ADR-0022 (`[dispatch_pct.*]` trace-following)

## Context

The supervisor supports multiple Claude accounts (one
`[[accounts]]` block per billing identity, each with its own
`CLAUDE_CONFIG_DIR`). The dispatch policy
(`runner.account_dispatch.choose_account`) is equal-priority across
accounts: when more than one has headroom, the one with the lowest
5h utilization wins. This is correct for *fresh* dispatches; a
task that has never run picks up the most-available account.

It is *incorrect* for *resumes*. Claude Code sessions are namespaced
by `CLAUDE_CONFIG_DIR`: the JSONL transcript lives under
`<config_dir>/projects/<slug>/<session_id>.jsonl`. A session created
under `personal` is invisible to `claude` invoked with the `work`
config dir and vice versa. When the orchestrator picks a different
account than the one that hosts the session, `claude --resume
<session_id>` exits in ~0.85s with:

```
No conversation found with session ID: <session_id>
```

The dispatcher classifies that as `error_during_execution` (no
matching environmental pattern), so it does NOT auto-retry. The
task burns an attempt and lands in `failed` even though nothing is
actually wrong with the work in progress.

### Reproducer (live, 2026-05-29)

In the `nlmixr2lib_ingestion` queue, task
`131-laffont_2025_nalmefene_naloxone`:

- Attempt 2 ran on `work`, created session
  `79e06baa-4004-4d73-9400-96b63dfc382d`, exited cleanly with
  sidecar 002.
- Operator answered the sidecar; force-dispatched with `--over-limit`
  while `work` was `slowing_down (89% 5h util)` and `personal` was
  `throttled_weekly (29% 5h)`.
- `choose_account` picked `personal` for attempt 3 (more 5h headroom).
- Attempt 3 spawned `claude --print --resume 79e06baa
  --config-dir /home/bill/.claude_personal/...` and exited in 0.85s
  with the `No conversation found …` error.
- Task → `failed`. No retry.

The bug is reproducible whenever the affined account is unavailable
and a different account has headroom. It is silent: the operator only
notices when they go looking at why a task landed in `failed`.

## Decision

Treat session affinity as a **correctness constraint**, not a policy
choice.

### 1. Record the affined account on the state YAML

`TaskState` gains a `session_account: str | None` field that the
dispatcher writes alongside `session_id` on every state transition
that produces a session. The existing per-run `RunRecord.account`
remains a complete attribution log; the new field is the cheap
"where is this session right now?" pointer.

Backwards compatibility: when the field is missing on an existing
state YAML, `TaskState.session_host_account()` falls back to scanning
`runs` newest-first and returning the last attempt's `account`. New
writes always populate the explicit field. Single-account queues
(no `[[accounts]]` list — legacy `[claude].config_dir` only) leave
`session_account` as `None` (one account → no affinity to honor).

### 2. Honor affinity in `choose_account`

The dispatch policy gains an `affined_account: str | None`
parameter. When it is set:

- If the account is no longer in `[[accounts]]`, the policy returns
  `None` with a clear reason. The supervisor's tick log surfaces
  this so the operator can see why the task is stuck.
- If the account is currently paused / throttled / at capacity,
  the policy returns `None` — same wait-and-retry behavior as
  "no eligible account."
- Otherwise the policy returns the affined account. **`task.account`
  pinning is ignored** when affinity is set; the pinning was applied
  at task-author time before the session existed, and the session's
  host is the binding constraint until an operator runs
  `queue restart-fresh` to clear it.

### 3. Force-dispatch honors affinity, too

`runner.force_dispatch.tick_consume` and `dispatch_synchronously`
both resolve the affined account before falling back to
`task.account` pinning. **`--over-limit` is a throttle bypass; it
is NOT a correctness bypass.** A force-dispatch to a non-affined
account would reproduce the live bug.

When the affined account is no longer configured, the request is
dropped with a log line that names `queue restart-fresh` as the
fix. The synchronous path raises `ForceDispatchError` with the same
message.

### 4. Operator escape hatch — `queue restart-fresh`

When the affined account is stuck for an extended period (e.g.
heavily weekly-throttled), the operator runs:

```
claude-task-runner queue restart-fresh <task_id>
```

This nulls both `session_id` and `session_account` on the state
YAML and resets `resume_attempts` to 0. The next dispatch picks
whichever account `choose_account` selects normally, creates a
fresh session, and continues from the original task prompt. The
trade-off is that cached context (turns, file reads, prior tool
results) is lost. The operator opts in explicitly when the
trade-off is worth it.

The command is idempotent: a task without a session is left
unchanged, with `noop=True` reported in the JSON output for
scripts.

### 5. Failure classifier — auto-retry the in-flight bug

The default `[failure_classifier].environmental_patterns` allowlist
gains:

```
"No conversation found with session ID"
```

This is forward-compatible with state YAMLs written before the
affinity fix: when a pre-fix task hits the error on a tick, the
supervisor classifies it as environmental and auto-resumes. The
next tick exposes the post-fix orchestrator which honors affinity
correctly — the task either dispatches to the right account (if
available) or waits.

Queue-side overrides remain queue-controlled; this only sets the
package default.

### 6. Doctor check — `orphaned_sessions`

Walks `state/*.yaml`; for each with `session_id != null`, resolves
the host account via `TaskState.session_host_account()` and
**WARN**s when it is not in `[[accounts]]`. Names each orphaned
task and recommends `queue restart-fresh`. WARN, not FAIL: the
queue isn't broken, but specific tasks can't move forward.

## Alternatives considered

- **Auto-null `session_id` after N consecutive ticks of affined
  account unavailability.** Rejected as the default. Silent context
  loss is the wrong default — a task that has done meaningful work
  shouldn't lose its history because the account it ran on hit a
  weekly cap that resets in 12 hours. The operator command is the
  right escape hatch; an automatic timeout is the wrong correctness
  guarantee. Could be added later as an opt-in setting if a queue
  needs it, but not in the default path.
- **Per-task `session_affinity_timeout_s` in the YAML.** Rejected
  for the same reason; same downside in a different costume.
- **Treat session affinity as advisory and retry on failure.**
  Rejected. Each failed cross-account resume burns ~0.85s and an
  `attempts` count against the per-task circuit breaker. Three of
  those (`failure_circuit_breaker_threshold = 3`) and the task is
  permanently failed. Correctness must come before policy here.
- **Re-derive the host account from `runs[-1].account` only (no
  explicit field).** Rejected: the runs list already accumulates
  failed cross-account attempts (the bug we're fixing). The most
  recent run's account is the one we LAST tried, not the one that
  hosts the session. The session host is the most recent run whose
  `session_id` was produced or successfully resumed — knowable from
  the runs list but requires more careful walking. An explicit
  field is one read instead of a walk-and-classify; the fallback
  path covers the legacy state YAMLs cleanly.

## Consequences

- (+) Multi-account queues stop burning attempts on cross-account
  resume errors. The headline reproducer (work → personal jump on
  task 131) no longer fails.
- (+) The explicit `session_account` field makes the runtime
  contract visible: an operator skim of a state YAML answers
  "which account is this on?" without scanning runs.
- (+) The failure classifier change is forward-compatible: old
  state YAMLs hitting the bug now auto-retry, and the orchestrator
  picks the right account on retry.
- (+) The doctor check surfaces orphaned sessions proactively
  rather than the operator discovering them via a stuck task.
- (-) Tasks affined to a heavily throttled account can stall
  longer than they would in the pre-fix code (the pre-fix code
  failed them immediately; the post-fix code waits). The operator
  command + the doctor check + the dispatch log line ("session
  affinity blocks dispatch: …") combine to make the stall visible.
- (-) New CLI verb (`queue restart-fresh`) to remember. Mitigated
  by the dispatch log line and the doctor remediation text both
  naming the command explicitly.

## Reversibility

High. The affinity logic is one parameter on one pure function;
passing `affined_account=None` (or never plumbing it through)
restores the pre-fix behavior. The `session_account` field is
additive and backwards-compatible: state YAMLs without it parse,
and the fallback `session_host_account()` derives the same value
from runs. The new `queue restart-fresh` command is additive. The
failure-classifier change is one allow-list entry an operator can
remove in their per-queue TOML.
