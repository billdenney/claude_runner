# ADR-0010: Effort levels are TOML-driven per model

- **Date:** 2026-05-03
- **Status:** accepted

## Context

Claude Code accepts effort levels (`low`, `medium`, `high`, `max`,
`extra_high`) that vary by model. Anthropic adds and removes levels over
time. Hardcoding the accepted set as a Literal type forces a code change
every time the API evolves.

## Decision

Effort levels are configured in TOML, per model:

```toml
[effort_levels]
"claude-opus-4-7"   = ["low", "medium", "high", "max", "extra_high"]
"claude-sonnet-4-6" = ["low", "medium", "high"]
"claude-haiku-4-5"  = ["low", "medium", "high"]
```

`Task.effort` is a plain `str` validated at load time by
`runner.effort_levels.validate_effort(model, effort)`. Unknown effort
levels for a given model raise `UnknownEffortLevel` with a clear message
listing the accepted set.

`claude-task-runner effort list <model>` prints the configured set.

## Alternatives considered

- **`Literal["low", "medium", "high", "max", "extra_high"]`:** rejected;
  forces code change on Anthropic API updates. Also loses model-specific
  validity (Sonnet doesn't accept `max`).
- **Free-form string with no validation:** rejected; typos go undetected
  until dispatch fails.

## Consequences

- (+) New effort levels = TOML edit, no code change.
- (+) Per-model validation catches misuses.
- (-) Slightly more setup (operator must know which effort levels their
  models support, and update TOML when Anthropic changes them).

## Reversibility

High. Switching to a hardcoded enum is a code change only.
