# 0009. Only `prompt` and `working_dir` are required in task YAML

- Status: accepted
- Date: 2026-04-19
- Deciders: @billdenney

## Context

Early drafts of the task schema required a half-dozen fields per file.
In practice this made the scaffolding CLI's output look intimidating
and pushed users to copy-and-modify rather than learning what each
field did. Most knobs (model, allowed tools, max_turns, effort) have
reasonable package-wide or project-wide defaults.

## Decision

Only `prompt` and `working_dir` are required. Every other field is
optional and resolved in this order:

1. Explicit value in the task YAML.
2. Derived from another field (e.g., `id` from filename, `title` from
   `prompt`, `max_turns` from `effort`).
3. User's `claude_runner.toml` (`default_model`, `default_effort`,
   `default_allowed_tools`).
4. Hard-coded package defaults in `defaults.py`.

A minimal, valid task file is two lines. The `claude_runner new`
command writes out all the fields explicitly so users can see exactly
what the runner will use, but those generated values are equivalent to
what the loader would have filled in from the minimal form.

## Alternatives considered

- **Require every field** — rejected: noisy, makes the tool feel heavy
  for a two-line task.
- **Only require `prompt`** — rejected: `working_dir` controls where
  Claude's file operations land, which is load-bearing and has no
  sensible project-wide default (cwd at run time is a footgun).

## Consequences

- **Positive:** the on-ramp is trivial — the 60-second quickstart in
  the README really is 60 seconds. Advanced users still have every
  knob.
- **Negative:** error messages from the loader have to be precise about
  which of the two required fields is missing; we surface that with
  filename and field in the ValueError.
- **Follow-ups:** if a future option proves to be frequently needed
  and has no reasonable default (e.g., a destructive-operations opt-in
  for safety-sensitive users), it should be added to the required
  list via a new ADR superseding this one.
