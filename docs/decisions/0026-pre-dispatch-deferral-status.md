# ADR-0026: Honor the pre-dispatch hook's exit-1 deferral contract (`deferred` status)

- **Date:** 2026-06-21
- **Status:** proposed
- **Related:** ADR-0012 (failure classifier + circuit breaker), ADR-0013
  (pre/post-dispatch hooks), ADR-0023 (`working_dir_template`)

## Context

The pre-dispatch hook has long documented an exit-code contract (see the
`nlmixr2lib_ingestion` queue's `setup_worktree.sh`):

- `exit 0` — readiness OK, worktree ready; dispatch proceeds.
- `exit 1` — task is **deferred**: a transient wait, e.g. an input paper
  flagged for operator re-acquisition (`needs_acquisition.jsonl`) or a
  pending `*_trimmed.md`. Not a task failure.
- other non-zero — **hard** failure (config/git bug); needs an operator.

The dispatcher did not honor it. To stop a perma-deferring hook from
re-attempting forever and starving the queue, an earlier fix
(`_record_pre_dispatch_failure`, routed through `_finalize_state`)
recorded **every** non-zero hook exit as `pre_dispatch_hook_failed` and
counted it toward the circuit breaker. So a paper merely *awaiting
re-acquisition* burned through `failure_circuit_breaker_threshold`
deferrals and died as `failed_circuit_breaker`.

Observed live (June 2026), five tasks circuit-broke purely on
re-acquisition deferrals — `zotero-009` (awaiting `PMID_22257150`),
`zotero-015`, `zotero-074`, `zotero-081`, and `frompeople-695`. Their
PDFs were acquired later, but the tasks stayed circuit-broken and never
re-dispatched; an operator had to hand-edit the state YAMLs.

The tension is real: we cannot simply stop counting deferrals (the
starvation the breaker was added to prevent returns), but a clean
deferral is genuinely not a failure.

## Decision

Honor the contract by splitting the non-zero pre-dispatch path:

- **`exit 1` and not timed out → transient DEFERRAL.** New
  `_record_pre_dispatch_deferral` parks the task in a new `deferred`
  lifecycle status. The deferral is deliberately **not** appended to
  `runs` (so `_count_trailing_failures` — hence the circuit breaker —
  never sees it) and does **not** consume an `attempts` slot. It sets
  `next_eligible_at = now + [failure_classifier].deferral_recheck_cooldown_s`
  (default 900 s) and stores `deferred_reason` (the hook stderr) for
  operator visibility.
- **any other non-zero, or a timeout → HARD failure.** Unchanged: routed
  through `_record_pre_dispatch_failure` → `_finalize_state`, counted
  toward the breaker so a permanently-broken hook still stops.

The orchestrator's `_eligible_candidates` treats `deferred` like
`awaiting_sidecar`: skipped while parked, re-eligible once
`next_eligible_at` elapses. Re-dispatch then **re-runs the hook** — if
the input is now ready it proceeds (worktree setup runs as usual); if
still blocked the dispatcher re-parks it with a fresh cooldown. The
cooldown bounds re-check frequency, so a long wait re-checks periodically
instead of being re-picked every tick — the anti-starvation property —
while never tripping the breaker.

## Consequences

- A task awaiting an external input now **parks** (re-checking on the
  cooldown) instead of dying — the correct behavior for the
  file-acquisition workflow this queue runs.
- New `deferred` status + three backward-compatible `TaskState` fields
  (`deferral_count`, `next_eligible_at`, `deferred_reason`; all
  defaulted, so legacy state YAMLs load unchanged). Operators get
  first-class visibility into why and until when a task is parked.
- Hard failures (config/git bugs, hook timeouts) still reach the breaker
  — the guarantee from ADR-0012 / the prior pre-hook fix is preserved.
- **Deploy note:** the supervisor must be restarted onto the new code as
  one step. Old code cannot *read* a `deferred` status (the `TaskStatus`
  Literal would reject it), but old code never *writes* one, so no
  mixed-version state exists until the first deploy. No migration needed.
- The cooldown is a per-queue setting (`[failure_classifier]
  .deferral_recheck_cooldown_s`) with a 15-minute default; queues that
  want snappier re-checks can lower it.
- The five live circuit-broken tasks were reset to `pending` by hand
  (their files had since arrived); this change prevents recurrence.
