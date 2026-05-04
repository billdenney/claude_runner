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
  operator answers. This skill lists, presents, and answers them.
---

# /runner-answer-sidecar — clear pending sidecar questions

The skill is optimized for **minimum operator typing**. Each question
maps 1:1 onto an `AskUserQuestion` invocation; the operator clicks
once per question and the response JSON gets written. No paraphrasing,
no manual editing.

## Steps

1. **List open sidecars.** Run
   `claude-task-runner sidecar list --queue <CWD> --json`. The output
   is `{"sidecars": [{task_id, sequence, summary, questions, ...}]}`.

2. **If empty**, tell the user: "No open sidecars." Stop here.

3. **If one or more**, walk through them ONE AT A TIME (don't try to
   batch). For each:

   a. **Show context.** Run
      `claude-task-runner sidecar show <task_id> <sequence> --queue <CWD> --json`
      and report:
      - `summary` (one line)
      - `context` (verbatim — may be multi-paragraph)
      - For each question: prompt, options (with `*` next to the
        recommended option), `multi_select` flag, `allow_free_text`
        flag.

   b. **Ask each question via `AskUserQuestion`.** This is a strict
      mapping — do NOT paraphrase or summarize:
      - The `AskUserQuestion.question` is the sidecar's prompt verbatim.
      - The `header` is short — derive from the question id.
      - The `options[]` come from the sidecar's options[]. Use
        `option.label` as the AskUserQuestion `label` and
        `option.description` (if present) as the `description`. The
        recommended option goes FIRST and gets " (Recommended)"
        appended to its label.
      - Set `multiSelect=true` if the sidecar question has
        `multi_select=true`.
      - DO NOT add an "Other" option manually — AskUserQuestion adds
        one automatically. The operator only types if `allow_free_text`
        is true AND they choose Other.

   c. **Collect answers.** For multi-select questions, the operator's
      answer is a list of values; for single-select, a single string.

   d. **Write the response.** Build a JSON list of
      `{id, value}` objects (one per question) and pass to:
      ```
      claude-task-runner sidecar answer <task_id> <sequence> \
          --queue <CWD> \
          --answers '[{"id":"q1","value":"A"},{"id":"q2","value":["X","Y"]}]'
      ```
      Optional: `--notes "<short operator note>"`. Default to empty
      if the operator didn't volunteer commentary.

4. **Move on to the next sidecar.** Repeat steps 3a–3d until the list
   is exhausted.

5. **Final summary.** Tell the user how many sidecars were answered
   and which task IDs are now unblocked.

## Things this skill does NOT do

- **Doesn't second-guess the operator.** Even if the recommended
  option seems "obviously right", let the operator pick.
- **Doesn't auto-fill notes.** Notes are blank by default; only set
  them if the operator says something specific in chat.
- **Doesn't restart the supervisor.** Once responses land, the next
  supervisor tick re-dispatches via `claude --resume`.
- **Doesn't read sidecars from other queues.** Each invocation is
  scoped to one `--queue` (default: cwd).

## Important nuance: free-text answers

If a question has `allow_free_text: true`, the operator can type a
custom answer via the AskUserQuestion "Other" path. When that
happens, pass the free-text string as the `value`. Don't try to
re-validate or sanitize — the runner accepts whatever string the
operator wrote.
