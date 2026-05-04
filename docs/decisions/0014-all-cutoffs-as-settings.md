# ADR-0014: Every cutoff is a TOML setting

- **Date:** 2026-05-03
- **Status:** accepted

## Context

The existing runner has dozens of magic numbers scattered through bash
scripts and Python source: 90% rate-limit threshold, 60s reporting
interval, 2h "stale running" threshold, 1500ms post-shortcuts pad in
expect, 10s/15s/5s capture phase timeouts, etc. Tuning any of them
requires a code change.

## Decision

**No magic numbers in runtime code paths.** Every threshold, timeout,
window size, percentile multiplier, retry cap, etc. lives in
`claude_task_runner/config/defaults/settings.toml`. Per-queue
`claude_runner.toml` overrides any default.

`claude-task-runner config show` prints the merged effective config.
`claude-task-runner config validate` checks the TOML against the schema
and warns on unknown keys.

The settings module loads once at startup; settings are passed (or
referenced via a `Settings` instance) into the components that consume
them. Settings are NEVER consulted ad-hoc in the middle of a hot loop —
each component receives its slice at construction time.

## Alternatives considered

- **Magic numbers with comments:** rejected; tuning requires PRs and
  redeployment.
- **Environment variables:** rejected; doesn't scale to ~30 tunables;
  hard to document and audit.

## Consequences

- (+) Operators tune behavior at runtime without rebuilding.
- (+) Configuration is auditable and version-controllable.
- (+) Tests can construct settings explicitly.
- (-) More boilerplate to plumb settings through components. Mitigated
  by passing structured `Settings` slices rather than individual values.

## Reversibility

Low — once cutoffs are settings, hardcoding them again is a regression
in flexibility. But the goal is permanence.
