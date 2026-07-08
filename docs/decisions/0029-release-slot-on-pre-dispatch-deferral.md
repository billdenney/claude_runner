# 0029 — Release the in-flight slot on a pre-dispatch deferral

Status: Accepted (2026-07-08)

- **Related:** ADR-0026 (exit-1 deferral → `deferred` status), ADR-0025
  (restart-survivable workers; the subprocess-leak guard this fixes was
  Bug 5 of that era's zombie-consolidation), ADR-0013 (pre/post-dispatch
  hooks), ADR-0024 (multi-account session affinity / per-account caps).

## Context

The orchestrator tracks concurrency with an in-memory
`dict[str, DispatchSlot]` (`in_flight_slots`). A slot is claimed when a
dispatch thread is spawned and released by `_reap_finished` on a later
tick, once that thread has exited. `_reap_finished` carries a
defence-in-depth guard (ADR-0025): if a slot's dispatch thread has
exited but the subprocess it spawned is *still alive* — a
SIGTERM→SIGKILL escalation that failed, typically a kernel D-state — it
refuses to free the slot and holds it until the pid disappears. It finds
that pid via `_recorded_subprocess_pid`, which returns `runs[-1].pid`
from the task's state YAML.

That guard silently assumed **`runs[-1]` is the run this dispatch thread
just wrote.** A pre-dispatch exit-1 *deferral* breaks the assumption. By
ADR-0026 a deferral parks the task in `deferred` **without spawning a
worker and without appending a `RunRecord`** (keeping it out of the
circuit-breaker's `runs` scan). So after a deferral, `runs[-1]` is a
**stale record from an earlier *real* dispatch**, and its `pid` is a
long-exited subprocess whose OS pid may since have been **recycled** by
an unrelated process — or be owned by another user, which `_pid_alive`
reports alive on `EPERM`. `_reap_finished` then reads the recycled pid,
concludes "subprocess leak," and **holds the slot forever.**

Impact scales catastrophically as `max_concurrency` shrinks. Observed
live (2026-07-08) on the `nlmixr2lib_ingestion` queue:

- The `work` account (`max_concurrency: 1`) had a single `deferred`
  task pinning its only slot. `account list` showed `work in_flight=1`
  with `5h=0%`; `supervisor.json` held the `deferred` task in
  `in_flight` across many ticks. Result: **0% dispatch for days** while
  614 runnable work-affined tasks waited behind 145 file-blocked ones
  that each grabbed the single slot and deferred. Two parked examples
  had `deferral_count` of 858 and 860, each with a real prior
  `runs[-1].pid` (`669862`, `3923817`).
- The `personal` account (`max_concurrency: 5`) survived only because a
  few free slots remained; a restart that re-dispatched 5 `deferred`
  orphans leaked 4 of personal's 5 slots.

## Decision

Two coordinated changes.

**1. (primary) Treat a deferral as spawning no subprocess.**
`_recorded_subprocess_pid` returns `None` when `state.status ==
"deferred"`. A deferral demonstrably spawned no worker (it exits in the
pre-dispatch hook phase, before any `claude` spawn, and dispatch writes
`status="running"` *before* spawning), so `runs[-1]` cannot describe
this dispatch's subprocess — there is nothing to leak-guard. The slot is
then freed on the next reap exactly like any dispatch that finished
without a surviving worker. This is airtight: `deferred` is written
*only* by the deferral path, so `status == "deferred"` ⟺ "the
just-finished dispatch spawned no subprocess." The real leak guard is
untouched for every status that *does* append a run
(`failed`/`completed`/`awaiting_sidecar`, whose `runs[-1]` is the
genuine just-finished run).

**2. (secondary) Make an operator's dispatch block visible to the
selector.** New opt-in `[dispatch].dispatch_block_file` (queue-relative,
default unset). When set, `_eligible_candidates` reads that JSONL and
skips any task whose id appears in an entry flagged `"block_dispatch":
true` — *without* spawning a dispatch the hook would only exit-1 defer.
This targets the `needs_acquisition.jsonl` convention's task-keyed
permanent blocks (a paper awaiting a supplement/upstream). Even with the
slot correctly freed, such a task is otherwise re-selected every
`deferral_recheck_cooldown_s`, dispatched, deferred, repeated — one
wasted dispatch cycle per cooldown that, on a 1-slot account, briefly
re-occupies the only slot. The reader is fail-safe: a missing file, a
malformed line, or a row without the flag yields no blocked id, so the
task dispatches and the hook enforces the block as before — a broken
block-list *under*-blocks (a wasted defer) rather than *over*-blocking
(stranding runnable work).

## Alternatives considered

- **Append a `pid=None` sentinel run on deferral** (so `runs[-1].pid` is
  `None`). Rejected: it re-enters `runs`, the exact structure ADR-0026
  deliberately keeps deferrals out of so the circuit breaker never
  counts them; exempting a sentinel again would spread deferral-awareness
  across `_count_trailing_failures` too. The status check is narrower and
  keeps ADR-0026's invariant intact.
- **Compare timestamps in `_reap_finished`** (only guard when
  `runs[-1].started_at >= slot.started_at`). More general — it catches
  any stale-`runs[-1]` outcome — but couples the pid helper to the slot
  and leans on cross-thread clock ordering. The deferral is the only
  outcome that fails to append a run, so the status check is both
  sufficient and easier to reason about. Kept in reserve if another
  non-appending outcome ever appears.
- **Part 2 via `target_path` rows / trim-blocked inputs.** Rejected for
  the selector: resolving a task's input files is queue-specific (the
  hook shells out to `task_inputs.py`); the runner cannot do it
  generically. Only the task-keyed `block_dispatch: true` rows map
  cleanly to the runner's task model, and they are precisely the
  *permanent* blocks where eliminating churn matters most.
- **Exponential deferral backoff scaled by `deferral_count`.** A viable,
  fully general churn reducer (no queue coupling) that would also cover
  trim/`target_path` deferrals. Deferred to a future ADR: it changes
  ADR-0026's fixed-cooldown semantics, whereas Part 1 already makes each
  defer *harmless to the slot* and Part 2 zeroes the churn for the worst
  (permanent) blockers.

## Consequences

- A `deferred` task's slot is released on the next reap. A single
  parked task can no longer pin a low-concurrency account; `work`
  dispatches its backlog again.
- `_recorded_subprocess_pid`'s documented `None` cases gain the deferral
  case; the genuine-leak behaviour is unchanged and now regression-tested
  from both directions (a `deferred` live-pid slot frees; a `running`
  live-pid slot still holds).
- Part 2 is inert for every queue that does not set
  `dispatch_block_file`; no behaviour change on upgrade. Reading a small
  JSONL once per tick is negligible.
- **No new state fields, no schema bump, no migration.** Part 1 reads an
  existing field; Part 2 adds one optional settings key. Old state YAMLs
  and `supervisor.json` load unchanged.
- **Deploy:** restart the supervisor onto the new code. The force-parked
  file-blocked tasks (an operator mitigation that set
  `next_eligible_at=2099`) should then be un-parked so they defer
  harmlessly again (see the PR runbook).

## Reversibility

High. Part 1 is a three-line guard; reverting restores the leak. Part 2
is disabled by leaving `dispatch_block_file` unset. Neither touches
persisted state, so a downgrade needs no migration.
