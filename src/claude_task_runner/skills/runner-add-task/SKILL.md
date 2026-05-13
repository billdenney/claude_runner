---
name: runner-add-task
description: |
  Use this skill when the user wants to add a task to the Claude task
  runner queue. Triggers: "/runner-add-task", "add a task to the
  queue", "queue up this work", "enqueue a job for the runner",
  "add a popPK extraction task", or any equivalent
  enqueue-this-for-the-runner phrasing. Walks the operator through
  required fields, validates effort against the per-model TOML, then
  invokes `claude-task-runner queue add` to write the YAML.
---

# /runner-add-task — add a task to the runner queue

This skill is interactive. It collects required fields via
`AskUserQuestion` (where there's a fixed list of choices) or by
asking directly (where there's free-form text), then invokes
`claude-task-runner queue add` to write the task YAML.

## Required fields

Drive the operator through these one at a time. Don't ask for them
all at once.

1. **Task ID** — a short identifier matching `[A-Za-z0-9._-]+`.
   Convention: numeric prefix matching the task's order in the queue
   (`007-name`). If the operator doesn't have a prefix in mind, run
   `claude-task-runner queue list --queue <CWD> --json`, look at the
   highest existing prefix, and propose the next number.

2. **Title** — single line. Ask plainly.

3. **Prompt** — the actual task text. For anything longer than ~3
   sentences, write to a temp file and pass `--prompt-file
   /tmp/<id>.prompt.txt`. Don't try to escape multi-line prompts on
   the CLI.

4. **Model** — `AskUserQuestion` with the configured models from
   `claude-task-runner usage whoami` (or hardcoded common set if you
   can't read it: `claude-opus-4-7`, `claude-sonnet-4-6`,
   `claude-haiku-4-5`). Default opus for substantive work.

5. **Effort** — `AskUserQuestion` with the per-model accepted set.
   Read `[effort_levels]` from the config: typically `low`, `medium`,
   `high`, `max`, `extra_high` for opus; smaller set for sonnet /
   haiku. The CLI will reject mismatched (model, effort) pairs.

6. **Priority** — `AskUserQuestion` with `low | normal | high`.
   Default `normal`.

## Optional fields (ask only if the user volunteers info)

- **Allowed tools** — comma-separated list. Default omits the flag,
  giving the dispatch the runner-default toolset.
- **Tags** — free-form labels. Useful for cohort grouping
  (`ema-cohort:foo` overrides the EMA bucket key).
- **`--weekly-critical`** — set if the user says it must complete
  before the weekly window resets.

## Invocation

Once collected, run something like:

```
claude-task-runner queue add \
    --queue <CWD> \
    --id 007-foo \
    --title "Extract Bar 2026 abciximab popPK" \
    --prompt-file /tmp/007-foo.prompt.txt \
    --model claude-opus-4-7 \
    --effort high \
    --priority normal
```

If the runner is already up, the supervisor will pick the task up on
the next poll. If not, suggest `claude-task-runner supervisor start`
or rely on the watchdog.

## Things this skill doesn't do

- Doesn't dispatch the task immediately; only writes the YAML.
- Doesn't ask about `depends_on` or per-task token caps unless the
  operator brings them up — defaults are sane.
- Doesn't validate the prompt content; if the operator wants the
  runner to extract a paper, the operator's prompt is the source of
  truth.
