# 0031 — Sidecar openness is accounted per QUESTION, not per file

Status: Accepted (2026-08-20)

- **Related:** ADR-0030 (mechanical readiness gates — `sidecar_response` is
  one of them), ADR-0027 (sidecar refile loop guard), ADR-0007 (fresh schema,
  no migration).

## Context

A sidecar request may ask several questions:

```json
{"task_id": "...", "questions": [{"id": "q1"}, {"id": "q2"}, {"id": "q3"}]}
```

The operator's response carries an `answers` array. Nothing checked that
every asked `id` appeared in it, and **every** "is this sidecar open?" test
in the codebase asked only whether a `response-NNN.json` file existed —
`queue.sidecar.list_open_sidecars`, and through it the readiness gate
(`runner.readiness`), the orchestrator's eligibility sweep, the dispatcher's
post-run status override, and the operator-facing `sidecar list` /
`fetch_all.sh` / `/runner-answer-sidecar`.

So a response answering only `q1` closed the request. `q2` and `q3` became
invisible to every counter simultaneously, and the task was released back to
the runner as if the operator had decided. There was no counter anywhere
that could have shown otherwise.

Measured in the `nlmixr2lib_ingestion` queue on 2026-08-09: **192 questions
across ~125 tasks unanswered**, a large share of them behind a response file
that answered only `q1`. The state was reported as "0 open sidecars" in good
faith more than once, because that is what every available count said.

## Decision

**1. Openness is per question.** A request is open while any asked
`questions[].id` is missing from its response's `answers[].id`. A request
that asks nothing (a `file_and_exit` notification) is closed by the presence
of a response file, and open while none exists.

`queue.sidecar.open_sidecars()` yields an `OpenSidecar` carrying
`outstanding`, `answered`, `partial` and `response_path`.
`list_open_sidecars()` remains as a `(task_id, sequence, request_path)`
projection of it, so the four runner-side consumers inherit the corrected
test without changing shape.

**2. The accounting reads ids, not whole payloads.** `asked_question_ids` /
`answered_question_ids` pull `questions[].id` and `answers[].id` from raw
JSON rather than validating through `SidecarRequest` / `SidecarResponse`.
Those models are `extra="forbid"` and validate everything, so cosmetic drift
that says nothing about answeredness — a legacy request missing
`created_at`, an answer carrying `notes` — would fail and force a request to
be classified with no evidence. On the live corpus, strict validation
rejected 166 requests that per-id reading classifies correctly.

**3. Undecidable is open, not closed.** A request whose asked ids cannot be
determined (unparseable JSON; a question entry with no id) is reported open
with `error` set, never silently closed. An unnameable *answer* entry is
different and is skipped rather than fatal: it credits nothing, so every
asked id stays outstanding — the conservative direction — and live responses
do contain such stubs (`{"id": "", "value": "A"}` beside a valid `q1`).

**4. Creation is gated, not just detection.** `sidecar answer` refuses
(exit 3) a response omitting any asked id. `--merge` carries forward the
recorded answers for ids not supplied, so topping up an already-partial
request stays a one-liner; `--allow-partial` is the explicit override and
leaves the sidecar open on the omitted ids.

**5. `sidecar list` names the outstanding ids** (`outstanding`, `answered`,
`partial`, plus `n_open` / `n_outstanding_questions`), because an operator
told only which task is stuck cannot tell what is still missing.

## Alternatives considered

- **Fix only the counts.** Rejected: the gap would keep being created. The
  refusal in `sidecar answer` is the durable half; the count fix only
  measures a backlog it cannot stop growing.
- **Validate request/response strictly and treat any failure as open.**
  Rejected on measurement: it would have surfaced 166 already-answered
  legacy requests as open, wedging that many tasks behind the readiness
  gate and burying the 53 genuinely-partial ones in noise.
- **Have `answer` merge silently whenever a response exists.** Rejected:
  implicit completion is the same class of behaviour as the original bug.
  `--merge` is explicit, and the rejection message names it.
- **Widen `_ID_KEYS` to prompt-text fields** (`question`, `prompt`, `text`)
  so legacy shapes stop erroring. Rejected: those are not identifiers;
  guessing one risks reporting a question answered when it was not.

## Consequences

- On the live queue the count moved from 73 open requests (file test) to
  **124 open requests / 166 outstanding questions**, of which **53 were
  partial and previously invisible**. Three genuinely-broken request files
  surface with `error` and need hand repair.
- Tasks holding a partial response now stay `awaiting_sidecar` instead of
  being re-dispatched with questions unanswered. That is the point, and it
  is also a behaviour change for any such task already in flight.
- `SidecarAnswer` gained `notes` and `SidecarOption` gained
  `proposed_names`; both are additive with defaults, so old files still
  load. `proposed_names` makes routine naming questions mechanically
  triageable — see the `agent-stop-and-ask` skill, which also requires any
  proposed canonical name to appear in backticks in the option label.
- `sidecar list --json` gained keys; it never removed any.

## Reversibility

High. The accounting lives behind `open_sidecars`; reverting
`list_open_sidecars` to the file-existence test restores the old behaviour
in one function. The `sidecar answer` gate is one call to
`_check_answers_cover_request`. No on-disk format changed — the two new
schema fields are optional and default to empty.
