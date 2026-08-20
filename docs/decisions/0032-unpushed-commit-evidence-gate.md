# 0032 — A committed-but-unpushed branch is not `completed`

Status: Accepted (2026-08-20)

- **Related:** ADR-0020 (gate `completed` on an observable output artifact —
  this extends it), ADR-0012 (failure classifier), ADR-0026 (pre-dispatch
  deferral status).

## Context

ADR-0020 gates `completed` on at least one observable artifact:

```python
has_commit or has_sidecar or has_deliverable
```

`has_commit` is satisfied the moment the worker runs `git commit`. Nothing
downstream checks whether the branch was ever **pushed**.

That matters because `completed` is terminal. From
`runner/orchestrator.py`:

```python
_DISPATCHABLE_STATUSES = frozenset({"pending", "failed"})
```

with `completed` listed among the explicitly-excluded statuses ("done"). A
task's YAML staying in `todo/` is not what makes it eligible — the state
record is. So a run marked `completed` is never re-selected, and if it
committed without pushing, the only copy of the work is a local branch tip:
one `git worktree remove` or `git branch -D` from silent loss, with no
remote to recover from.

Workers land in exactly that state routinely, because the repo's gates are
slow. `buildModelDb()` runs ~25 minutes; `devtools::check()` and a testthat
sweep over ~1700 models are longer. The idiomatic worker behaviour is to
commit a checkpoint and then end the turn waiting for the gate to return.
Their own final messages say so:

> "I'll push once it returns clean"
> "Waiting on `check()` before pushing"
> "I'll wait for the background `check()` to notify rather than polling.
> Ending this turn"
> "Registry rebuild ⏳ running (~1667 models) | Vignette render gate ⏸
> blocked on the registry"

Measured on the `nlmixr2lib_ingestion` queue, 2026-08-20: **51 branches**
carried complete extractions — model files, vignettes, `NEWS.md` entries,
regenerated registry artifacts — spanning 2026-07-29 to that day. **None
had ever run `git push`.** 50 were recorded `status: completed`. Of 3471
YAMLs in `todo/`, exactly one task was dispatchable; the 51 were
indistinguishable from ~3060 genuinely-finished tasks in the same state.

The uncomfortable part: **19 of the 51 reached that state through an
earlier attempt that failed `uncommitted_work_left`.** That guard (added
2026-07-27/30, after 23 models were nearly lost across 5 worktrees) teaches
workers to commit before their gates finish. It converted a loud failure
into a quiet one — the work moved from "untracked in a worktree" to
"committed to a branch nobody will ever push", and the second state *passes*
every check the first one failed.

## Decision

Extend the ADR-0020 evidence check with a third stage. A clean-exit run in a
worktree is **not** `completed` when the task branch carries commits that
exist on no remote ref. Such a run becomes `failed` with stop_reason
`unpushed_commit_left`.

`failed` is in `_DISPATCHABLE_STATUSES`, so the task returns to the pool,
resumes in its worktree, finishes its gates and pushes. The existing
consecutive-failure circuit breaker bounds the retries, exactly as it does
for `uncommitted_work_left`.

Detection is `git rev-list --oneline HEAD --not --remotes` — the canonical
"on no remote" query. `git push` updates the local remote-tracking ref
itself, so a branch pushed during the run reads as pushed with no fetch
required.

**Guard:** when the repository has no remote-tracking refs at all, the check
returns empty and never fires. Without that, every commit in a local-only
checkout would read as unpushed and the gate would fail every run.

The three stages now read as one progression, each a step further along the
path from "work exists" to "work is safe":

| stage | stop_reason | the work is… |
|---|---|---|
| ADR-0020 | `end_turn_no_output` | not there at all |
| uncommitted guard | `uncommitted_work_left` | in the worktree, untracked |
| this ADR | `unpushed_commit_left` | on a branch, on no remote |

## Alternatives considered

**Require a push to satisfy `has_commit`.** Rejected: it conflates "produced
output" with "delivered output", and would make a legitimate
commit-then-ask-a-sidecar run look like it produced nothing.

**Have the orchestrator re-scan `completed` tasks for unpushed branches.**
Rejected: it puts git knowledge in the scheduler, and it re-litigates a
decision at every tick rather than recording it once where the evidence was
actually gathered.

**Push automatically on the worker's behalf at end of run.** Rejected: the
worker deliberately withholds the push until its gates are green. Pushing
for it would publish unverified work — the opposite of what the deferral
means.

## Consequences

- A worker that commits and stops mid-gate now costs one extra dispatch
  instead of silently stranding its work. That is the intended trade.
- A task that genuinely cannot push (missing credentials, rejected
  non-fast-forward) will retry to the circuit-breaker threshold and then
  surface as `failed_circuit_breaker` — visible, which it was not before.
- A run whose branch carried unpushed commits from a *previous* attempt is
  also flagged, even if this run only wrote a report. That is deliberate:
  the loss condition is a property of the branch, not of who created it.
- Existing state records are not migrated. The 51 already-stranded tasks
  need their status flipped to `pending` by hand (or by a one-off script) to
  re-enter the pool; the gate only prevents new occurrences.

## Reversibility

Delete the `left_commits_unpushed` branch in `_finalize_state` and the
`unpushed` field on `OutputEvidence`. No schema migration: `stop_reason` is
a free-form `str`, and no persisted record shape changes.
