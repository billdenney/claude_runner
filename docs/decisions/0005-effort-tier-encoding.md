# 0005. `effort` tier encoding (off/low/medium/high)

- Status: accepted
- Date: 2026-04-19
- Deciders: @wdenney

## Context

Claude has two knobs that control how much work a single turn can do:
the extended-thinking `budget_tokens` and the `max_turns` cap. Exposing
those raw makes users learn the SDK; bundling them into a named tier
matches how most users actually reason about tasks ("this is a small
job", "this one is hairy").

We also need a reasonable seed value for the budget controller's
per-task token estimate so the scheduler can size concurrency before
the EMA has observed any real runs.

## Decision

Introduce an `effort` field with the tiers `off`, `low`, `medium`,
`high`. Each tier expands to a concrete `(thinking_budget_tokens,
max_turns, estimated_input_tokens)` triple defined in one place,
`defaults.py`:

| effort | thinking_budget | max_turns | estimate |
|--------|-----------------|-----------|----------|
| off    | 0               | 10        | 8 000    |
| low    | 2 000           | 20        | 25 000   |
| medium | 8 000           | 40        | 80 000   |
| high   | 24 000          | 80        | 250 000  |

Explicit YAML values for `max_turns` or `estimated_input_tokens` always
win over the tier defaults.

## Alternatives considered

- **Raw `thinking_budget` field** — rejected: forces users to think in
  token counts and to couple their decision to a specific model's
  pricing.
- **Numeric 1–5 scale** — rejected: numerically arbitrary; named tiers
  self-document in code and YAML.
- **Derive effort from prompt length** — rejected: brittle and surprising.

## Consequences

- **Positive:** a single field covers both "how hard should Claude
  think?" and "how much budget should the scheduler reserve for this
  task?". New users can ignore both knobs entirely and still get
  sensible behavior.
- **Negative:** four tiers is a coarse-grained knob. If the tiers need
  re-tuning we must update the table in `defaults.py` and the README in
  lockstep — but that concentration of truth is the point.
- **Follow-ups:** if real-world usage shows the tiers are
  mis-calibrated, supersede this ADR with the new numbers rather than
  quietly editing the table.
