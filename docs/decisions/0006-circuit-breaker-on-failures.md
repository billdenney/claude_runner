# 0006. Circuit breaker halts runs on 3 consecutive or >50% rolling failures

- Status: accepted
- Date: 2026-04-19
- Deciders: @billdenney

## Context

A natural failure mode of a long-running queue is "one task broke
everything" — e.g., a bad config that makes every task fail the same
way. Without a stop condition the runner would keep spending tokens
producing the same failure forever. But a single bad task should not
halt unrelated work either: the queue must be tolerant of the normal
rate of individual errors.

## Decision

Wrap the scheduler in a circuit breaker with two tripwires, both
config-tunable:

1. **Consecutive failures** — default 3. Three failures in a row stops
   the run.
2. **Rolling failure rate** — default: >50% failure over the last 10
   completions (with at least 4 samples). Indicates sustained trouble
   even when individual streaks are short.

A failure is any of: non-success `ResultMessage`, `Stop` hook with a
non-success reason, or an unhandled SDK/subprocess exception. User
interrupts (SIGINT/SIGTERM) and budget-imposed pauses are not failures.
When the breaker trips, the scheduler finishes in-flight tasks, emits
`circuit_breaker_tripped`, and exits with a non-zero status code.

## Alternatives considered

- **No breaker; rely on the user to Ctrl-C** — rejected: the whole point
  of this package is unattended runs.
- **Only consecutive-failure trip** — rejected: misses the "flaky but
  not back-to-back" failure mode.
- **Fail-fast on the first error** — rejected: incompatible with the
  explicit requirement that "subsequent tasks should continue when one
  crashes".

## Consequences

- **Positive:** unattended runs are safe to leave going; a broken queue
  surfaces quickly rather than burning tokens indefinitely.
- **Negative:** two thresholds means two knobs to tune. Defaults were
  chosen to be forgiving (a third of a burst is fine) while still
  catching a truly broken queue within ~10 tasks.
- **Follow-ups:** `claude_runner status` should surface the breaker
  state alongside task status so operators know why a run stopped.
