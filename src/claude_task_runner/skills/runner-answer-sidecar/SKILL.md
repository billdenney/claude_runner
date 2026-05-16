---
name: runner-answer-sidecar
description: |
  Use this skill when the user wants to answer pending sidecar
  questions from the Claude task runner. Triggers:
  "/runner-answer-sidecar", "answer sidecars", "what sidecars are
  open?", "any sidecars waiting?", "resolve the questions", "the
  runner is asking me something". A "sidecar" is the runner's
  stop-and-ask protocol — when a dispatched task hits a question it
  can't autonomously decide (a covariate encoding ambiguity, a
  missing parameter, etc.) it pauses and writes a JSON request the
  operator answers. This skill lists, presents, and answers them
  in a single batched pass.
---

# /runner-answer-sidecar — clear pending sidecar questions

The skill is optimized for **minimum operator typing AND minimum
agent-loop iteration**. Every open sidecar is fetched in one shell
pass via the bundled `fetch_all.sh` helper script, presented to the
operator in batched `AskUserQuestion` calls (up to 4 questions per
call), and answered in a single batch of `claude-task-runner sidecar
answer` invocations. This avoids the back-and-forth of fetching →
asking → answering → fetching the next one, which both costs
agent-loop turns and lets new sidecars sneak in mid-pass.

## Steps

1. **Bulk-fetch every open sidecar's context.** Run the bundled helper
   from the skill directory:

   ```bash
   bash /home/bill/.claude/skills/runner-answer-sidecar/fetch_all.sh \
       --queue <CWD>
   ```

   Output is a single JSON document with shape:
   ```json
   {
     "queue": "<dir>",
     "n_open": <int>,
     "sidecars": [
       {"task_id": "...", "sequence": N,
        "summary": "...", "context": "...",
        "questions": [{"id": "...", "prompt": "...", "options": [...],
                       "multi_select": <bool>, "allow_free_text": <bool>,
                       "recommended": "..."}]},
       ...
     ]
   }
   ```

   The script tolerates v1-schema (legacy) sidecar requests by reading
   the raw file directly and synthesising a v2-shaped record with a
   `schema_warning` field so the agent can still present them.

2. **If `n_open == 0`**, tell the user "No open sidecars." and stop.

3. **Present a one-paragraph summary per sidecar to the operator.**
   Don't dump the full context block — it's often multi-paragraph and
   redundant once `summary` is in hand. Tabulate task_id ↔ summary so
   the operator sees the scope of the batch before answering.

4. **Build the AskUserQuestion batches.** Count the total number of
   `questions[]` across all sidecars (some sidecars have 2–4 questions;
   most have 1). Group them into batches of **at most 4** questions per
   `AskUserQuestion` call (that's the tool's hard cap). Order them to
   keep questions from the same sidecar contiguous when possible — the
   operator can then answer related multi-question sidecars without
   losing context.

   For each `AskUserQuestion` question:
   - The `question` is the sidecar's `prompt` verbatim (or a slight
     paraphrase if too long for the UI; never paraphrase a recommended
     option's substance).
   - The `header` is short (≤ 12 chars) — derive from the sidecar's
     drug + year, or task_id, or question id.
   - The `options[]` come from the sidecar's `options[]`. Use
     `option.label` as the AskUserQuestion `label` and
     `option.description` (truncated to ~200 chars if long) as the
     `description`. The recommended option goes FIRST and gets
     " (Recommended)" appended to its label.
   - Set `multiSelect: true` if the sidecar question has
     `multi_select: true`.
   - DO NOT add an "Other" option manually — AskUserQuestion adds one
     automatically. The operator only types if `allow_free_text` is
     true AND they pick Other.

5. **Submit all answers in a single batched run.** After the operator
   answers all batches, build one shell block that submits every
   sidecar response:

   ```bash
   QUEUE=<queue_path>
   claude-task-runner sidecar answer <tid_1> <seq_1> --queue $QUEUE \
       --answers '[{"id":"q1","value":"<answer1>"}]'
   claude-task-runner sidecar answer <tid_2> <seq_2> --queue $QUEUE \
       --answers '[{"id":"q1","value":"<answer1>"},{"id":"q2","value":"<answer2>"}]'
   ...
   ```

   For multi-select questions, `value` is a JSON array of the chosen
   option values. For free-text "Other" answers, `value` is the
   operator's free-text string verbatim — no validation, no sanitization.

6. **Verify the queue is clear.** Run `claude-task-runner sidecar list
   --queue <CWD> --json` once more and confirm `n_open == 0`. If new
   sidecars opened during the answer pass (the supervisor may dispatch
   while you're working), repeat from Step 1 — the operator can decide
   whether to handle this round or stop.

7. **Final summary.** Emit a markdown table:

   | # | Task | Decision |
   |---|---|---|
   | 1 | <task_id_1> | <one-line answer summary> |
   | ... |

## Things this skill does NOT do

- **Doesn't second-guess the operator.** Even if the recommended
  option seems "obviously right", let the operator pick.
- **Doesn't auto-fill notes.** Notes are blank by default; only set
  them via `--notes` if the operator says something specific in chat.
- **Doesn't restart the supervisor.** Once responses land, the next
  supervisor tick re-dispatches via `claude --resume`.
- **Doesn't read sidecars from other queues.** Each invocation is
  scoped to one `--queue` (default: cwd).
- **Doesn't paraphrase the operator's free-text answers.** When a
  question has `allow_free_text: true` and the operator types
  something via "Other", pass the string verbatim to `--answers`.

## Important nuances

### Upstream-missing default

A common `/extract-literature-model` sidecar shape: the current
paper's PK is fixed to an upstream paper not on disk; the agent asks
whether to skip, drop-and-re-dispatch, or queue the upstream as a
`depends_on` task. **The recommended response is "queue upstream + add
depends_on"** — find the upstream PDF if possible, otherwise place a
``_needs_acquisition.flag`` so the operator can drop the PDF later.
This pattern is general-purpose (the depends_on chain + the
`failed`/`pending` re-dispatch flow are core runner machinery) but
the helper that wires it up is queue-specific because the
OA-acquisition ladder and the upstream-detection cadence are
domain knowledge that lives in the queue.

For queues that ship a queue-local helper at
``<queue>/_scripts/handle_upstream_dependency.py`` (e.g. the
nlmixr2lib popPK ingestion queue), the recommended response when
answering an upstream-missing sidecar is:

1. **If the upstream is identifiable** (PMID, DOI, or unambiguous
   citation), run the queue-local helper:

   ```sh
   python3 <queue>/_scripts/handle_upstream_dependency.py \
       --queue-dir <queue path> \
       --current-task-id "<downstream task id>" \
       --upstream-pmid <PMID>             # or --upstream-doi <DOI>
       --upstream-citation '<citation>' \
       --upstream-drug <drug>
   ```

   The helper: tries the OA-PDF ladder (queue-specific); on success
   drops a `trim_queue` marker; on failure writes a
   `_needs_acquisition.flag`. Either way it queues the upstream as a
   new task in `todo/` and edits the downstream task's YAML to add
   `depends_on`. After it completes, answer the sidecar with the
   "queue upstream + depends_on" option (typically option A; check
   the request for exact value).

2. **If the upstream is genuinely unidentifiable** (e.g. internal
   study, in-house simulator output, no PMID/DOI), surface the
   sidecar's "skip / inline with Errata / defer" options to the
   operator as written; do not invoke the helper.

3. **If the queue has no `_scripts/handle_upstream_dependency.py`**
   helper, do the same flow manually: drop the PDF (or
   `_needs_acquisition.flag`) at the queue's expected papers/ path,
   write a new upstream task YAML at the next free slot in `todo/`,
   and edit the downstream task's YAML to add `depends_on`.

### v1-schema (legacy) sidecars

Older worktree skill versions sometimes write sidecar requests in the
v1 schema (with `question`/`options` flat fields and `request_id`
instead of `sequence`). The runner's `sidecar show` command rejects
these on validation, but `fetch_all.sh` reads the raw file directly
and synthesises a v2-shaped record. The synthesised record will carry
a `schema_warning` field so you know the response will need to be
written manually if `sidecar answer` rejects it; in practice the
runner's `sidecar answer` accepts answers against legacy requests as
long as the `--answers` JSON has the right `id` keys (the v1 schema's
options have `id`, e.g. "A"/"B"/"C", which map directly to v2's
`option.value`).

### Batch sizing for AskUserQuestion

The `AskUserQuestion` tool caps at 4 questions per call. With N
sidecars having varying question counts:

- Total questions = sum of `len(sidecar.questions)` across all open
  sidecars.
- Batches needed = ceil(total / 4).
- Order: keep multi-question sidecars contiguous (don't split a
  sidecar's q1 across one batch and q2 into the next batch unless the
  total questions exceed 4 within that one sidecar).

### When the supervisor opens new sidecars during the pass

If `n_open` goes UP between Step 1 and Step 6 (the supervisor
dispatched more tasks while the operator was answering), the new
sidecars will show in the verification list. Decide with the operator
whether to process the new round now or stop and let them pile.

### Multi-select vs free-text

- `multi_select: true` → operator's answer is a JSON array of values.
- `allow_free_text: true` → operator may type a custom string; pass
  it as `value` directly.
- Both can be true simultaneously (operator picks zero-or-more from
  the menu, plus a free-text addendum). In practice the runner accepts
  whatever JSON you pass.
