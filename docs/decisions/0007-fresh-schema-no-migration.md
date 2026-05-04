# ADR-0007: Fresh schema; no migration of old runner data

- **Date:** 2026-05-03
- **Status:** accepted

## Context

The existing `claude_runner_queue/.claude_runner/` contains 127 task state
files, sidecars, and an events stream from prior work. The new runner
could read those formats directly (with adapters), or start fresh.

Operator confirmed: the existing work is complete; no live tasks need
to migrate.

## Decision

The new runner uses a fresh on-disk schema under `<queue>/.claude_task_runner/`
with `schema_version: 2` on every persisted file. Old `.claude_runner/`
directories are left in place untouched as historical reference. **No
migration script** is provided.

## Alternatives considered

- **One-shot v1→v2 migration script:** rejected as unnecessary work given
  no live tasks; would also constrain the v2 schema design.
- **Read-compatibility adapter:** rejected for the same reasons; adds
  ongoing maintenance burden for a transition that's already done.

## Consequences

- (+) Schema is designed for the new runner's needs without legacy
  constraints.
- (+) No migration code to maintain.
- (-) Old data is not visible through the new tool. If it ever needs to
  be queried, operator reads YAMLs directly.

## Reversibility

Medium. A one-shot migration could be written later if needed; the cost
is the cost of writing it then.
