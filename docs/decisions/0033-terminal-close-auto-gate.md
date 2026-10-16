# 0033 — A terminal close writes its own dispatch gate

Status: Accepted (2026-09-04)

- **Related:** ADR-0020 (gate `completed` on an observable output artifact —
  this reuses its evidence), ADR-0029 (the `block_dispatch` register the
  selector reads), ADR-0031 (sidecar openness per question, which the
  2026-10-15 amendment reads), ADR-0032 (unpushed-commit gate, the sibling
  case).

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

## Amendment (2026-10-15) — a run that asked a question is not a terminal close

**What happened.** As of 2026-10-15, 110 unfinished tasks on the nlmixr2lib
queue carried an `AUTO_GATED` row. 102 of them were `awaiting_sidecar` on
sidecars that asked real questions, and the operator had already answered
them; 84 of those answers called for more work (naming rulings, encoding
choices, "extract both models"). None of the 102 had run since September.
The answers never reached them.

**Why.** The decision above keys the gate on the run's output alone: a
deliverable, no commit, a clean worktree. That is also the shape of an
ordinary stop-and-ask, where the agent writes its report, files a request
with questions and ends its turn. `_finalize_state` wrote the row, and
`_apply_open_sidecar` then parked the task `awaiting_sidecar`. Once the
response arrived the sidecar-resume path would have admitted the task, but
the selector skips a task with a `block_dispatch` row before it looks at
anything else. The non-finding in the Context above already said that
re-dispatch after an answered question is the system working; the heuristic
did not encode it.

**Decision.** `terminal_gate.ensure_terminal_gate` writes no row while the
task has an open sidecar request with at least one question outstanding,
counted per question as in ADR-0031 (`queue.sidecar.open_sidecars`, read for
this task only). A request counts as asking too when it or its response
cannot be read well enough to tell what is answered: it stays open until an
operator repairs it, and may be asking something. The row is still written
as before:

- when the run leaves no open sidecar, including a rerun after the answer
  (e.g. "skip") that closes without asking again, which is a real terminal
  close;
- when the only open sidecar is a request with no questions, since
  acknowledging it would otherwise re-dispatch a task with nothing left to
  do, which is the case this ADR exists for.

Task status, the ADR-0020 evidence and every other branch of
`_finalize_state` are unchanged.

**Assumption: a request with no questions is a notification.** The v2
request schema has no notification marker; asks are filed as `questions`,
as the agent-stop-and-ask skill specifies. Free-form asks outside
`questions` existed. As of 2026-10-15 the queue held 184 question-less
requests among 2,331. 53 carried a free-form ask key (`options`,
`decisions_requested`, `sidecar_ask`, `question_to_operator`, ...); 44 of
those held a value, and the files of 43 of the 44 date from May or June
2026, the last from 2026-06-27. The 44th (dated 2026-09-08) is a completion
notice phrased as a question. The other 9 set those keys to null, an
explicit "no question". Every one of the 53 was answered. A schema test would not separate the two
kinds: only 9 of the 184 question-less requests validate as v2 at all,
because notifications carry their own extra keys (`kind`, `status`,
`verdict`, ...). If free-form asks come back, this assumption is the thing
to revisit.

**Finding the rows written before this change.** They stay in the register
until removed. The ones this amendment would not have written are the rows
with `status: AUTO_GATED` whose task is `awaiting_sidecar`, whose newest
sidecar request asks at least one question, and which has no open request
(`claude-task-runner sidecar list` does not list it). Removing such a row,
or setting its `block_dispatch` to false, lets the selector dispatch the
task with the answer it is waiting for.

**Reversibility.** Removing the `unanswered_asks` check from
`ensure_terminal_gate` restores the earlier gate exactly. The row format and
the register are unchanged, so rows written before and after this change
read the same.
