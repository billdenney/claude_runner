# ADR-0005: Try `claude --resume`, fall back to fresh restart

- **Date:** 2026-05-03
- **Status:** accepted

## Context

When the 5-hour window resets mid-task, the in-flight task either
(a) keeps running (we let it finish naturally, which works because we
throttle pre-emptively at 90%), or (b) failed at the rate-limit boundary
and needs to be re-dispatched in the new window.

Option (b) wastes tokens if we re-prompt from scratch — the cache is cold
and the model has to re-read the entire paper / source files.
`claude --resume <session_id>` reuses the prior conversation state.

## Decision

`runner.session.resume_or_fresh(task)`:

1. If `task.session_id` is set and the session JSONL file exists at
   `~/.claude/projects/<proj>/<session_id>.jsonl`:
   - Spawn `claude --resume <session_id> --print "Continue where you left off."`
   - If process exits with a `--resume`-specific error within
     `session.resume_fail_fast_s` seconds, fall through.
2. Fresh: spawn with original prompt; capture new session_id; update task.

`task.resume_attempts` is incremented per try and capped at
`session.max_resume_attempts` (default 3). Beyond cap, only fresh restarts
are attempted.

## Alternatives considered

- **Always fresh restart:** wastes cache; slower; more expensive.
- **Always resume (no fallback):** fails when session JSONL is missing
  (e.g., disk cleanup, machine swap) — task gets stuck.

## Consequences

- (+) Cheapest cross-window continuation when sessions are healthy.
- (+) Robust to session-file loss.
- (-) Slightly more code than a single strategy. Worth it for the cost
  savings and reliability.

## Reversibility

High. Setting `max_resume_attempts = 0` makes the system always-fresh.
