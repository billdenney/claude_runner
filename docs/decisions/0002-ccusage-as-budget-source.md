# 0002. Use `ccusage` as the primary budget source

- Status: accepted
- Date: 2026-04-19
- Deciders: @billdenney

## Context

Under the Pro/Max/Teams regime the Claude Agent SDK does not surface
rate-limit headers to Python callers, and the exact quotas are not
documented. The scheduler nevertheless needs a trustworthy reading of
"how much of the 5-hour window have I used already?" before it picks a
concurrency level.

[`ccusage`](https://github.com/ryoppippi/ccusage) is a community tool that
reads the same `~/.claude/projects/*.jsonl` transcripts Claude Code
writes and produces per-session and per-day token totals. It is the
closest thing to an authoritative view of the subscriber's own usage.

## Decision

The default `budget_source` is `ccusage`. The controller calls
`ccusage blocks --json` to read the active 5-hour window's total and
`ccusage daily --json` to sum tokens since the current week's anchor.
When `ccusage` is not on `PATH`, the package falls back to parsing
`claude /context` output. API-regime users bypass both and query
`anthropic-ratelimit-*` headers via a tiny probe request.

## Alternatives considered

- **Issue a probe via the raw `anthropic` SDK to read headers every time**
  — rejected for Pro/Max because those headers reflect a different limit
  tier than the subscription caps; the numbers would mislead.
- **Tail `~/.claude/projects/*.jsonl` ourselves** — rejected: duplicates
  what `ccusage` already does well, and would require tracking upstream
  schema changes.
- **Require the user to declare the budget statically** — rejected as the
  default because it can't detect real-world usage from other Claude
  sessions on the same account, but it remains supported as
  `budget_source = "static"`.

## Consequences

- **Positive:** the controller has an accurate, near-real-time read of
  aggregate usage across every Claude process the user runs.
- **Negative:** adds an optional external dependency. We address that by
  falling back gracefully when `ccusage` is absent.
- **Follow-ups:** if `ccusage` output changes shape, the parser in
  `budget/sources/ccusage.py` needs updating; a lightweight integration
  test against the current `ccusage` release format would catch this.
