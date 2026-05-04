# ADR-0001: Full rewrite vs. wrap third-party claude-runner

- **Date:** 2026-05-03
- **Status:** accepted

## Context

The existing queue at `mab_human_consensus/claude_runner_queue/` runs on a
third-party `claude-runner` Python CLI plus bash wrappers. It has accumulated
debt that's hard to retrofit:

- No proactive usage tracking (crashes into rate-limit walls)
- Subprocess hangs with no detection short of a 2h heuristic
- An asyncio backend that's incompatible with `--thinking` in claude CLI 2.1.85
- A bash+expect `/usage` parser with zero tests
- No window scheduling, no skill layer, no decision log

## Decision

Build `claude_task_runner` as a **full replacement** of `claude-runner`
plus the bash wrappers and `claude-usage`. Single new Python package, single
new on-disk schema, no compatibility shim with the old CLI.

## Alternatives considered

- **Wrap and incrementally improve** the existing `claude-runner`. Rejected:
  inherits the asyncio/subprocess complexity, the bug surface, and the
  inability to tightly integrate proactive usage tracking with dispatch
  decisions. Bug-fixing the third-party tool would consume the budget of
  building the right thing.
- **Phased rewrite (Phase A: usage lib, Phase B: supervisor wraps existing,
  Phase C: replace core)**. Rejected because the user explicitly chose
  full replacement to avoid carrying old design constraints forward.

## Consequences

- (+) Clean architecture aligned with proactive throttling and skill-driven
  operator interaction.
- (+) No carry-over of asyncio/subprocess bugs.
- (-) Largest scope; longest until first usable version.
- (-) The existing `claude-runner` repo (also authored by the user) becomes
  unused. Its lessons inform this design.

## Reversibility

Low. Once `claude_task_runner` is the system of record, switching back to
`claude-runner` would mean re-migrating data and losing the new feature set.
