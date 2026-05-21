# Diagnosis: `130-lowe_2009_omalizumab` attempt 3 completed with zero output

- **Reported:** 2026-05-21
- **Task:** `130-lowe_2009_omalizumab` on the `nlmixr2lib_ingestion` queue
- **Window:** 2026-05-20 21:25:25–21:32:48 UTC (~7 minutes, attempt 3)
- **Status assigned by runner:** `completed`, `stop_reason: end_turn`
- **Output observed by operator:** none (no commit, no deliverable, no sidecar, no report, no streamjson log)

## Timeline (from state YAML)

| Attempt | Window (UTC) | Duration | stop_reason | error | Output tokens |
|--------:|---|---|---|---|---|
| 1 | 21:20:17 → 21:20:17 | < 1 ms | `pre_dispatch_hook_failed` | `DEFERRED: awaiting trim for PMID_19660004.pdf` | 0 |
| 2 | 21:21:54 → 21:23:43 | 109 s | `process_exit_nonzero` | `claude exited with code 143 and no result event` (systemd SIGTERM during service restart at 21:23:43) | partial |
| 3 | 21:25:25 → 21:32:48 | 443 s | **`end_turn`** | `null` | **28,336** |

Attempt 3 burned ~$2.29, ran a clean `end_turn` result event, and
produced no filesystem side effects.

## Where streamjson logs should be vs where they are

**They are nowhere.** Searched the runner source tree:

- `runner/dispatcher.py:369-377` opens the `claude` subprocess with
  `stdout=subprocess.PIPE` and pipes it straight into
  `stream.parse_lines` (lines 217). The raw bytes are consumed but
  never tee'd to a file.
- `runner/stream.py` is a pure in-memory parser.
- `<queue>/.claude_task_runner/logs/` is created by
  `queue.store.queue_runtime_dir` but no code writes per-task
  streamjson into it.

So the operator's observation that no log exists for this task is
correct — but it's not specific to this task. **No task gets a
streamjson log on disk today.** A separate change would be needed
to start tee-ing the stream for forensic value.

## Is `completed` the correct status? No.

`runner/dispatcher.py:486-545` (`_finalize_state`) decides status
purely from `stop_reason`:

```python
elif run.error is None and run.stop_reason in ("end_turn", "result"):
    new_status = "completed"
```

There is no check on:

- a new commit on the worktree branch,
- an open sidecar,
- a declared deliverable existing.

The post-dispatch hook is the natural enforcement point but is
configured empty on this queue (`post_dispatch_command = ""` in
`claude_runner.toml`).

A `completed` status here is wrong from the operator's perspective:
the deliverable does not exist. The runner needs a content gate, not
just a `stop_reason` gate.

## Root-cause hypothesis (ranked)

### 1. `working_dir: null` on the task YAML (highest-probability)

`<queue>/todo/130-lowe_2009_omalizumab.yaml` line 63:
`working_dir: null`. Every other comparable task points
`working_dir` at the per-task worktree path. Consequences:

- `dispatcher.py:375`: `cwd=str(task.working_dir) if task.working_dir else None`
  — the `claude` subprocess inherits the supervisor's cwd (the queue
  dir), not the worktree path.
- `runner/hooks.py:62`: `TASK_WORKING_DIR=""` in the env, and
  `_scripts/hooks/setup_worktree.sh:109-112` then bails with a
  "skipping" log line but exits 0 — dispatch proceeds.
- With cwd = queue dir, the agent's relative file writes don't
  land in the worktree. Worktree confirms zero changes.

### 2. Silent agent refusal that emitted only a final assistant message
Less likely. 28k output tokens is consistent with the agent doing
work in the wrong directory rather than with a "I cannot proceed"
refusal. A refusal would also typically be < 1k tokens.

### 3. Acquisition/trim gate edge case
Not the cause — by attempt 3 both `PMID_19660004.pdf` and the trimmed
markdown existed, and `needs_acquisition.jsonl` has no relevant
lines. Just context for attempt 1's deferral.

## Suggested gating (minimal, future PR)

Before `_finalize_state` returns `"completed"`, require at least one
of:

a) a new git commit on the worktree branch since dispatch start (one
`git rev-list --count <pre-dispatch-sha>..HEAD` call),
b) an open sidecar request (already detected; would just re-use the
existing scan),
c) a declared deliverable path (`task.deliverable_paths`) that exists.

If none match, mark `failed` with
`stop_reason = "end_turn_no_output"`. The failure-classifier circuit
breaker handles repeated misses normally.

Skip the check when `task.working_dir is None` (preserve current
behavior for non-worktree tasks).

See `docs/decisions/0020-gate-completed-status-on-output-artifact.md`
for the full proposed ADR with implementation plan and unit-test
coverage.

## Recommended follow-ups (not in this PR)

1. **Validate `working_dir` at enqueue time** in `queue.schema.Task`:
   forbid `working_dir: null` for queues that configure a
   worktree-creating pre-dispatch hook. Or fall back to a deterministic
   per-queue default. The operator authored this task with `null`
   without the schema flagging it.
2. **Implement ADR-0020** (this PR proposes it; doesn't implement it).
3. **Tee streamjson to disk** for forensic value when an attempt
   completes oddly. Separate ADR — not all logs are interesting; an
   on-error capture would minimise disk usage.
