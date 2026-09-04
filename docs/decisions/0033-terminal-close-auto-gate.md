# 0033 — A terminal close writes its own dispatch gate

Status: Accepted (2026-09-04)

- **Related:** ADR-0020 (gate `completed` on an observable output artifact —
  this reuses its evidence), ADR-0029 (the `block_dispatch` register the
  selector reads), ADR-0032 (unpushed-commit gate, the sibling case).

## Context

A run that ends as a clean SKIP or DEFER writes its deliverable report,
commits nothing, and leaves the worktree clean. ADR-0020 marks that
`completed`, and `completed` is not in `_DISPATCHABLE_STATUSES`, so on its
own such a task never re-fires.

The leak is the sidecar-resume path in `runner/orchestrator.py`. A task that
filed a sidecar sits in `awaiting_sidecar`, and the moment every request has
a matching response the orchestrator makes it eligible again. That is
deliberate and correct — it is how a task collects an operator ruling and
acts on it. But when the disposition was **terminal**, there is nothing to
act on: the task re-derives the same verdict at full effort and files the
same sidecar again.

Measured on the nlmixr2lib queue:

- `oare_PMC6930853` (Schoemaker 2019, a software-benchmark paper) was acked
  as a skip on 2026-09-02 and re-fired 24h later at `effort: high`, reaching
  the identical verdict on the identical evidence.
- `oare_PMC9823018` (a content-free open-access correction notice) did the
  same thing.

The selector reads only the `block_dispatch` register (ADR-0029), so a row
there is the one thing that actually holds such a task down. Until now
nothing wrote one except an operator by hand — a rule living only in skill
prose, which is exactly the kind that keeps being missed.

An important non-finding shaped the scope. A first diagnosis claimed ~35
tasks from one ack batch had re-fired wastefully. Measuring it: 28 ungated
tasks did re-dispatch after their acks, but **every one** followed an answer
to a real question — naming ratifications and encoding rulings — and 27 of
28 then completed. Zero were zero-question notification acks. The
question-answering re-dispatch is the system working. Only the terminal
close is the defect, and this ADR is scoped to it.

## Decision

When the ADR-0020 evidence shows a terminal close — `has_deliverable` and
NOT `has_commit`, with nothing uncommitted and nothing unpushed —
`_finalize_state` appends a `block_dispatch` row for the task to the
configured `[dispatch].dispatch_block_file`.

The status is **unchanged**: a terminal close is a genuine completion, not a
failure. Flipping it to `failed` would be actively harmful, because `failed`
IS dispatchable — it would convert a silent re-fire into an immediate one.

Deliberately conservative:

- Fires only on the unambiguous shape. A run that committed is not gated
  (gating it would strand real work behind a block the selector honours),
  and a run that left work uncommitted is not gated — that task *must* be
  re-dispatched to finish, and ADR-0020's `uncommitted_work_left` still
  fires for it.
- Never overwrites or duplicates an existing row, so an operator's curated
  ruling always wins.
- Writes `status: AUTO_GATED` plus `signals.auto_gated`, so rows this wrote
  are distinguishable from curated ones and can be audited or reversed in
  bulk if the heuristic is ever wrong.
- Swallows any write failure: a register problem must never fail a run that
  genuinely succeeded. The task simply stays ungated, as it was before.
- No-ops when `dispatch_block_file` is unset, so queues that do not use the
  convention are unaffected.

## Consequences

A terminal disposition gates itself at the moment it is reached, instead of
depending on an operator remembering to file a row. The cost of forgetting
was one full-effort re-derivation per paper, on a queue where that is an
`effort: high` opus dispatch.

The auto-written row is a *gate*, not a ruling. It records that the run
closed terminally and points at the deliverable; it does not claim to know
whether the right disposition was PERMANENT_SKIP or DEFER. An operator
reviewing the deliverable should replace it with a curated row carrying the
real disposition — and because auto rows are marked, finding them is a
one-line query.
