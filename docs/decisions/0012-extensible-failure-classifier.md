# ADR-0012: Extensible failure classifier via TOML patterns

- **Date:** 2026-05-03
- **Status:** accepted

## Context

The existing runner has a regex pattern set hardcoded in `run.sh`
(rate-limit messages, HTTP 5xx, ECONNRESET, etc.). Operator-deferred
errors are matched against a separate hardcoded set. Project-specific
failure modes (e.g., R `devtools::check` failures in the mAb queue) can't
be classified without modifying `run.sh`.

## Decision

Failure patterns are configurable in TOML:

```toml
[failure_classifier]
environmental_patterns = ["you've hit your limit", "hit your org", ...]
operator_patterns      = ["Operator: defer", "Abort: only the abstract", ...]
task_patterns          = []  # project-specific, e.g. "compilation failed"
```

`runner.retry.classify(error_text, settings)` checks pattern lists in
this precedence: operator > task > environmental > unknown.

Built-in defaults cover the existing patterns from `run.sh`.

## Alternatives considered

- **Hardcoded patterns (status quo):** rejected; project-specific failure
  modes can't be added without code changes.
- **Plugin entry points:** more flexibility but heavier to implement and
  document; TOML is sufficient.

## Consequences

- (+) Project-specific failure handling without code changes.
- (+) Defaults remain conservative; operators opt-in to project-specific
  patterns.
- (-) Operators must be careful with regex precedence (operator vs.
  environmental) — conflicting patterns could misclassify.

## Reversibility

High. Reverting to hardcoded patterns is a 1-line change in
`runner.retry.classify`.

## Update (2026-09-26)

Nothing ever called `runner.retry.classify` or `should_auto_resume`. The
orchestrator re-dispatches every `failed` task whatever its error text, and
the circuit breaker (`[failure_classifier].failure_circuit_breaker_threshold`
consecutive failed runs) is what stops the retries. So
`environmental_patterns`, `operator_patterns` and `task_patterns` changed
nothing: an `Operator: defer` failure was retried like any other. The two
functions and the three lists have been removed, and the loader rejects a
queue TOML that still sets a list with a message saying to delete it.
Deleting them changes nothing. The rest of `[failure_classifier]` (the
circuit-breaker threshold, `deferral_recheck_cooldown_s` and
`sidecar_refile_loop_threshold`) is unchanged.
