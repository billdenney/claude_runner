# ADR-0020: Gate `completed` status on at least one observable output artifact

- **Date:** 2026-05-21
- **Status:** proposed
- **Supersedes:** none
- **Related:** ADR-0012 (failure classifier), ADR-0019 (pre-init claude config)

## Context

On 2026-05-20 the live `nlmixr2lib_ingestion` queue marked task
`130-lowe_2009_omalizumab` as `status: completed` despite zero
externally-observable output:

- Worktree at `/home/bill/github/nlmixr2/nlmixr2lib/.claude/worktrees/130-lowe_2009_omalizumab/`
  was clean; no `claude/130-lowe_2009_omalizumab`-specific commit.
- No deliverable at `inst/modeldb/specificDrugs/Lowe_2009_omalizumab.R`.
- No sidecar request under `<queue>/.claude_task_runner/sidecar/130-lowe_2009_omalizumab/`.
- No report at `<queue>/reports/130-lowe_2009_omalizumab.md`.

The attempt's state YAML records:
- `stop_reason: end_turn`
- `error: null`
- `output_tokens: 28,336`
- `cost_usd: ~$2.29`
- `duration_s: 443`

So the agent ran for 7 minutes, generated 28k output tokens, the
`claude` subprocess exited with a clean `end_turn` result event, and
the runner judged the attempt successful. By every metric the runner
tracks, this WAS a successful attempt. By every metric the operator
cares about (commit, deliverable, sidecar), it was a no-op.

### Why the runner can't see the gap today

`runner.dispatcher._finalize_state` decides `status` purely from
`stop_reason`:

```python
elif run.error is None and run.stop_reason in ("end_turn", "result"):
    new_status = "completed"
```

There is no check on:
- Whether a new commit exists on the task's branch.
- Whether a sidecar request was filed.
- Whether a declared deliverable path exists on disk.

The post-dispatch hook is the natural place for a per-queue check —
on the live queue it is configured as `post_dispatch_command = ""`
(no hook), so the empty-output case slips through.

### Root cause of this particular incident (informational, not in
scope here)

The task's `working_dir: null` in
`<queue>/todo/130-lowe_2009_omalizumab.yaml` meant the dispatcher
spawned `claude` with `cwd = <supervisor's cwd> = <queue dir>`,
NOT the worktree path. The setup_worktree hook
(`<queue>/_scripts/hooks/setup_worktree.sh:109-112`) explicitly
bails when `$TASK_WORKING_DIR` is empty, but exits 0, so dispatch
proceeded. The agent operated in the queue dir, where it
presumably either wrote files that don't materialise in the
worktree, hallucinated success, or chose to exit without writing
anywhere. Either way the runner could not tell.

**Note on streamjson logs:** the operator observed "no streamjson
log under `<queue>/.claude_task_runner/logs/` for this task." That
is correct but misleading — the runner does not currently capture
the per-task streamjson log to disk for any task. The dispatcher
pipes the subprocess's stdout straight into `stream.parse_lines`
(`runner/dispatcher.py:369-377`) and discards the raw bytes. The
`logs/` directory is created by `queue_runtime_dir` but is empty
for every task. A separate ADR may opt to start tee-ing the stream
to disk for forensic value; that is independent of this ADR.

## Decision

`runner.dispatcher._finalize_state` will gate the `completed` status
on at least one of:

1. **A new git commit** on the worktree's branch since the dispatch
   started. Detected via `git rev-list --count <pre-dispatch-sha>..HEAD`
   inside `task.working_dir`. The dispatcher already snapshots
   `started_at`; we'll snapshot the pre-dispatch HEAD SHA at the same
   point in `dispatch()` (right before invoking `Popen`).
2. **An open sidecar request** for this task. We already detect this
   via `list_open_sidecars(queue_dir)` and override the status to
   `awaiting_sidecar` (`dispatcher.py:428-430`); the check here is
   the same set membership.
3. **A declared deliverable path** existing on disk. Tasks may opt in
   via a new `deliverable_paths: list[Path]` field on `Task`
   (relative to `working_dir`); the dispatcher consults the list when
   no commit and no sidecar were observed.

If none of (1), (2), (3) are true and the run otherwise looks
successful, the status becomes `failed` (not `failed_circuit_breaker`)
with `stop_reason = "end_turn_no_output"` and an `error` message
naming the missed gates. The circuit breaker still counts repeat
failures the same way; perma-no-output tasks reach
`failed_circuit_breaker` after `failure_circuit_breaker_threshold`
attempts.

The check is skipped (existing behavior preserved) when
`task.working_dir is None` — the runner has no anchor for a commit
check, and adding the check would break the use case of tasks that
intentionally run without a worktree (research / analysis tasks).
Those tasks need to opt in to a `deliverable_paths` declaration to
get gating.

## Consequences

**Pro:**
- The Lowe 2009 failure mode (agent runs cleanly, writes nothing)
  flips to `failed` instead of `completed`, blocking the queue from
  treating zero-output runs as success and freeing the operator
  from auditing every "completed" state YAML by hand.
- Circuit breaker eventually parks pathological tasks in
  `failed_circuit_breaker`, matching the existing handling of
  pre-dispatch-hook deferrals (`docs/decisions/0012-extensible-failure-classifier.md`).
- Per-task deliverable opt-in keeps the gate from forcing every
  task author to start producing commits.

**Con:**
- A new dispatcher dependency on `git` for the commit check
  (already implicitly required by the setup_worktree hook, but the
  runner core does not invoke `git` today).
- One more `subprocess.run` per dispatch on the post-run path. Small
  cost (single `git rev-list --count` call, milliseconds).
- Tasks that intentionally succeed without a commit (e.g. "summarise
  these PDFs and exit") would need to declare a deliverable_path or
  open a sidecar; otherwise they trip the new gate.

## Out of scope here

- Streamjson log capture to disk (separate ADR).
- Validation that `working_dir` is non-empty at task enqueue time
  (separate validation change in `queue.schema`).
- Backfill of historical "completed but empty" states.

## Implementation plan

1. Add `deliverable_paths: list[Path]` to `Task` schema (default `[]`).
2. Add `_snapshot_pre_dispatch_sha(working_dir)` helper to dispatcher.
3. In `_finalize_state`, before returning `new_status = "completed"`,
   run `_verify_output_evidence(...)`. On no evidence, set
   `new_status = "failed"` with `stop_reason = "end_turn_no_output"`.
4. Unit tests:
   - Task with commit ⇒ `completed`.
   - Task with sidecar ⇒ `awaiting_sidecar` (unchanged path).
   - Task with declared deliverable that exists ⇒ `completed`.
   - Task with no commit, no sidecar, no deliverable ⇒ `failed`.
   - Task with `working_dir is None` ⇒ existing behavior (no gating).
