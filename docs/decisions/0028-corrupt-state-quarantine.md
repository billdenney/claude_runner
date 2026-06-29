# 0028 — Corrupt-state-file quarantine

Status: Accepted (2026-06-29)

## Context

Every task's lifecycle lives in a `TaskState` YAML under
`<queue>/.claude_task_runner/state/<task_id>.yaml`. A crash, OOM-kill, or
**power-loss** that interrupts a write can leave that file **unparseable** —
truncated mid-write, or (the shape seen in the wild) a stale multi-line-string
fragment left wedged after a shorter rewrite that didn't truncate.

Every code path that loads state files handles a parse failure the same way —
log a warning and `continue`:

- `supervisor.reconcile.reconcile_orphans` (startup orphan demotion, ADR-0025)
- `supervisor.adoption.adopt_running_workers` (startup adoption, ADR-0025)
- `supervisor.reconcile_silent` (silent-orphan reaper, startup + per-tick)
- `runner.orchestrator` (the dispatch-candidate scan)

So a corrupt state file is **silently skipped by everything, forever**: the
task is never reconciled, never re-dispatched, never finished, and its id/slot
is wedged. `doctor`'s `state_yamls` check *flags* them (FAIL), but that is a
manual diagnostic — nothing recovers them at runtime.

Observed live (2026-06-29, after a power outage prompted an audit): 5 state
files (`frompeople-695-…`, `zotero-009/015/074/081-…`) had been corrupt since a
2026-06-21 kill-mid-write and silently skipped for a week — `status: pending`
tasks that simply never ran again.

Note the root-cause write path is **already atomic** (`write_state_atomic` =
tempfile + `fsync` + `os.replace`), so a *running* supervisor does not create
new corruption. The residue is historical / external (a kill during a
pre-atomic write, a bad disk, an outage during the rename window on a
non-ordered filesystem). The gap is purely *recovery*, not prevention.

## Decision

Add a dedicated recovery layer, `supervisor.reconcile_corrupt`
(`quarantine_corrupt_state_files`), wired into supervisor **startup before all
other recovery passes** (gated by `[supervisor].quarantine_corrupt_state`,
default `true`). For each state file that fails to parse:

1. **Quarantine** it via atomic `os.replace` into
   `state/.corrupt/<task_id>.<UTC-stamp>.yaml`. `queue.store.list_state_files`
   globs `state/*.yaml` *non-recursively*, so the dot-subdir is invisible to
   every subsequent scan — the file is preserved for forensics but gone from
   all sweeps.
2. The task now has **no** state file, which is the `pending` baseline
   (`orchestrator._DISPATCHABLE_STATUSES` includes `pending`), so it
   re-dispatches on the normal path.
3. **Salvage exception:** if a terminal `completed` status is readable from the
   intact head of the corrupt file, write a fresh minimal valid state
   preserving `completed` (`stop_reason="corrupt_state_quarantined"`) instead,
   so a finished extraction is not redone. Only `completed` is salvaged —
   re-dispatching a `failed` / `failed_circuit_breaker` / parked task is
   harmless or desirable; redoing a finished task wastes a whole run.

Startup is the load-bearing trigger because corruption is a crash/outage
artifact and a fresh supervisor always starts after one. A transient
`QueueIOError` (file vanished mid-walk, permissions) is NOT treated as
corruption — it is left for the next pass.

## Consequences

- **Self-healing after a crash / power outage.** Corrupt state no longer wedges
  a task indefinitely; the next supervisor start quarantines it and the task
  re-dispatches (or is preserved if it had completed).
- **Auditable.** Quarantined files accumulate under `state/.corrupt/` with a
  timestamped name; the supervisor logs a `corrupt_state_quarantined` event and
  an operator notification per file. `doctor`'s `state_yamls` check goes back to
  PASS once the dir is clean.
- **Rare false re-dispatch.** A task whose status was non-`completed` at
  corruption time re-runs from scratch (session_id can't be trusted from a
  corrupt file). Acceptable: corruption is rare, and extraction tasks are
  idempotent (the skill dedup-checks already-extracted models).
- **Steady-state is startup-only by design.** A running supervisor writes
  atomically and cannot create new corruption, so there is no per-tick scan;
  anything that does appear mid-run is caught at the next restart. (A per-tick
  pass on the steady-state reap cadence is a cheap future extension if external
  corruption ever proves to happen while running.)
- The legacy silent-skip is restorable via
  `[supervisor].quarantine_corrupt_state = false`.
