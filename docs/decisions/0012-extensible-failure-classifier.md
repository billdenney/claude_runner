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
