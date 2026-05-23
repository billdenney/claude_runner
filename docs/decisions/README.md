# Architecture Decision Records (ADRs)

Append-only log of architectural decisions. Each decision has its own
numbered file. ADRs are never edited after acceptance, except to mark
`Status: superseded by ADR-NNNN` when a later ADR replaces them.

## Index

| # | Title | Status | Date |
|---|-------|--------|------|
| 0001 | Full rewrite vs. wrap third-party claude-runner | accepted | 2026-05-03 |
| 0002 | Long-running supervisor + cron/systemd watchdog | accepted | 2026-05-03 |
| 0003 | Pip-installable sibling repo location | accepted | 2026-05-03 |
| 0004 | Three-band throttle (70/90) with EMA prediction | accepted | 2026-05-03 |
| 0005 | Try `claude --resume`, fall back to fresh restart | accepted | 2026-05-03 |
| 0006 | Pause weekly at threshold + end-of-week push | accepted | 2026-05-03 |
| 0007 | Fresh schema; no migration of old runner data | accepted | 2026-05-03 |
| 0008 | Use pexpect, not raw pty + subprocess | accepted | 2026-05-03 |
| 0009 | Inject Clock protocol everywhere (testability) | accepted | 2026-05-03 |
| 0010 | Effort levels are TOML-driven per model | accepted | 2026-05-03 |
| 0011 | EMA-driven concurrency tuning (re-enabled) | accepted | 2026-05-03 |
| 0012 | Extensible failure classifier via TOML patterns | accepted | 2026-05-03 |
| 0013 | Pre/post-dispatch shell hooks (replaces built-in worktree mgmt) | accepted | 2026-05-03 |
| 0014 | Every cutoff is a TOML setting | accepted | 2026-05-03 |
| 0015 | Time-of-day band modulation + nighttime-biased EOW push | accepted | 2026-05-13 |
| 0016 | Dynamic weekly pacing curve anchored to OAuth reset | accepted | 2026-05-13 |
| 0017 | Cleanup after the superseded `claude_runner` audit | accepted | 2026-05-13 |
| 0018 | Inject TERM and PATH into the generated systemd-user unit | accepted | 2026-05-14 |
| 0019 | Pre-init `.claude.json` before every claude spawn | accepted | 2026-05-18 |
| 0020 | Gate `completed` status on at least one observable output artifact | proposed | 2026-05-21 |
| 0021 | Per-account long-lived OAuth token via `claude setup-token` | proposed | 2026-05-23 |

## Template

```markdown
# ADR-NNNN: <short title>

- **Date:** YYYY-MM-DD
- **Status:** proposed | accepted | superseded by ADR-NNNN | deprecated

## Context

What problem prompted this decision? What constraints applied?

## Decision

What was decided?

## Alternatives considered

What was rejected and why?

## Consequences

What follows from this — positive, negative, neutral?

## Reversibility

How hard would it be to undo? (low / medium / high)
```

Use `claude-task-runner adr new "<title>"` to scaffold a new ADR with the
next number and template filled in.
