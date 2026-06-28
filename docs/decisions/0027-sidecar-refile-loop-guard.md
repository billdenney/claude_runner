# 0027 — Sidecar re-file loop guard

Status: Accepted (2026-06-28)

## Context

A task that hits a stop-and-ask trigger files a sidecar request and exits
`awaiting_sidecar`. Once the operator answers (every request has a matching
response), the orchestrator makes the task dispatchable again
(`orchestrator._eligible_candidates`, the `awaiting_sidecar` re-eligibility
branch) and it re-dispatches.

If the re-dispatched run cannot actually act on the answer — the answer needs an
upstream fix that never lands, a supplement that never arrives, or the agent
simply re-asks the same unresolved question — it files **another** sidecar and
goes back to `awaiting_sidecar`. The operator answers again, it re-dispatches
again, and the task loops: `answered → re-dispatch → re-file → answered → …`,
the sidecar sequence climbing (request-001, -002, -003 …) with no progress.

Observed live (2026-06-28): `zotero-083`, `frompeople-1380-rao`, and a large
batch of reference-harvested tasks whose lead PDF was never acquired. The
queue-side fix for the *file/supplement* class is a `block_dispatch` flag the
pre-dispatch hook (`setup_worktree.sh`) honours — it parks the task as
`deferred` before the agent runs. But that only covers blockers expressed as an
acquisition entry; a *modeling* sidecar re-filed without progress is not caught,
and nothing bounds the loop generically.

The failure circuit breaker (ADR-0012) does not help: each looping run "succeeds"
(it produces an observable artifact — the new sidecar — so the ADR-0020 output
gate passes), so no `failed` is recorded and the breaker never trips.

## Decision

Add a **dispatcher-side sidecar re-file loop guard**.

- New `TaskState.sidecar_refile_count` (int, default 0): consecutive sidecars
  filed with **no commit** in between.
- New `failure_classifier.sidecar_refile_loop_threshold` (int, default **4**).
- In the dispatcher, when a finalized run is about to be marked
  `awaiting_sidecar` (it left an open sidecar): compute whether the run made
  progress — `made_progress = _new_commit_since(task.working_dir, pre_sha)`.
  - `made_progress` → reset `sidecar_refile_count` to 0 (legitimate
    ask → build → ask; the counter never accumulates for productive tasks).
  - otherwise → increment it.
  - if it reaches the threshold → set status `failed_circuit_breaker`,
    stop_reason `sidecar_refile_loop`, with an operator-facing `error`, instead
    of `awaiting_sidecar`. The orchestrator then leaves it parked for operator
    intervention rather than re-dispatching.

The arithmetic is isolated in the pure helper
`dispatcher._sidecar_refile_decision(prior_count, made_progress, threshold) ->
(new_count, tripped)` so it is unit-testable without driving a full dispatch.

The guard is applied on the main dispatch finalization path only; the adopt
(restart-recovery, ADR-0025) path leaves `awaiting_sidecar` as-is, because the
loop always re-dispatches through the main path where the count is maintained.

## Consequences

- A stuck answered-sidecar loop now terminates after `threshold` no-progress
  cycles and surfaces to the operator as `failed_circuit_breaker`
  (stop_reason `sidecar_refile_loop`) instead of consuming cap forever.
- The commit-based reset means a legitimate multi-question task that commits
  between questions is never penalised. The only residual false-positive is a
  task that files ≥ `threshold` sidecars *before its first commit*; the default
  of 4 is above the typical pre-build question count, and the threshold is
  configurable.
- Complementary to, not a replacement for, the queue-side `block_dispatch`
  hook check: that parks known file/supplement/upstream blockers early (as
  `deferred`, recoverable); this is the generic backstop for everything else
  (as `failed_circuit_breaker`, operator-cleared).

## Alternatives considered

- **Total sidecar cap (no commit-reset).** Simpler, but penalises legitimate
  multi-question tasks that ask several questions before building.
- **Require-consumption-step** (agent must explicitly acknowledge it consumed
  the prior answer before re-dispatch). More precise but invasive — needs a new
  agent-side protocol step.
- **Block re-dispatch on any answered sidecar.** Too aggressive — it breaks the
  normal, desired answered → re-dispatch flow for the common single-question
  case.
