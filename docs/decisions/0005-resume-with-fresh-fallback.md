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

`runner.session.plan_next_spawn(task, state, *, settings, …)` returns a
`SpawnPlan` (`strategy` ∈ `{RESUME, FRESH}`):

> **Amended (2026-06-13):** the entry point was named `resume_or_fresh(task)`
> in this ADR's original draft. It now lives at
> `runner.session.plan_next_spawn` and takes the `Task` plus its `TaskState`
> (session id and attempt counters live on the state, not the task) and a
> `SessionSettings` slice, returning a `SpawnPlan`. The decision logic below
> is unchanged in substance.

1. If `state.session_id` is set and the session JSONL file exists at
   `~/.claude/projects/<proj>/<session_id>.jsonl`:
   - Spawn `claude --resume <session_id> --print "Continue where you left off."`
   - If process exits with a `--resume`-specific error within
     `session.resume_fail_fast_s` seconds, fall through.
2. Fresh: spawn with original prompt; capture new session_id; update the
   task's state.

`state.resume_attempts` is incremented per try and capped at
`[session].max_resume_attempts` (default 3). Beyond cap, only fresh restarts
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

## Update (2026-09-25)

The fast fall-through in step 1 was never built. The dispatcher runs one
`claude --resume` per attempt and never turns a failed resume into a
fresh spawn within that attempt. Every resume attempt, successful or
not, increments `resume_attempts`. Once the count reaches
`[session].max_resume_attempts`, `runner.session.plan_next_spawn` plans a
fresh dispatch, and a resume whose session JSONL is missing goes fresh
at once. `[session].resume_fail_fast_s` existed only for the unbuilt
step, and it has been removed, along with the never-called
`fall_through_to_fresh`. The loader rejects a queue TOML that still sets
the key, with a message saying to delete it. `tests/integration/test_dispatcher.py`
(`TestResumeAttemptCounting`) pins the behaviour described here.
