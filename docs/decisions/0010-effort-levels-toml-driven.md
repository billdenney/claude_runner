# ADR-0010: Effort levels are TOML-driven per model

- **Date:** 2026-05-03
- **Status:** accepted

## Context

Claude Code accepts effort levels (`low`, `medium`, `high`, `max`,
`extra_high`) that vary by model. Anthropic adds and removes levels over
time. Hardcoding the accepted set as a Literal type forces a code change
every time the API evolves.

## Decision

Effort levels are configured in TOML, per model:

```toml
[effort_levels]
"claude-opus-4-7"   = ["low", "medium", "high", "max", "extra_high"]
"claude-sonnet-4-6" = ["low", "medium", "high"]
"claude-haiku-4-5"  = ["low", "medium", "high"]
```

`Task.effort` is a plain `str` validated at load time by
`runner.effort_levels.validate_effort(model, effort)`. Unknown effort
levels for a given model raise `UnknownEffortLevel` with a clear message
listing the accepted set.

`claude-task-runner effort list <model>` prints the configured set.

## Alternatives considered

- **`Literal["low", "medium", "high", "max", "extra_high"]`:** rejected;
  forces code change on Anthropic API updates. Also loses model-specific
  validity (Sonnet doesn't accept `max`).
- **Free-form string with no validation:** rejected; typos go undetected
  until dispatch fails.

## Consequences

- (+) New effort levels = TOML edit, no code change.
- (+) Per-model validation catches misuses.
- (-) Slightly more setup (operator must know which effort levels their
  models support, and update TOML when Anthropic changes them).

## Reversibility

High. Switching to a hardcoded enum is a code change only.

## Update (2026-09-25)

The `effort list` subcommand in the Decision section was never built;
the CLI has no `effort` group. The accepted set for each model is the
`[effort_levels]` table (package defaults in
`config/defaults/settings.toml`, overridable per queue), and
`claude-task-runner queue add` rejects a mismatched pair with an
`UnknownEffortLevel` error that lists the accepted set.

## Update (2026-09-26)

"Validated at load time" was never true. `Task.effort` cannot be
validated when a task YAML loads: `queue.store.load_task` has no settings,
and the accepted sets are the merged `[effort_levels]`. Until now only
`queue add` checked the pair, so a hand-written or edited task naming an
effort its model does not accept, or a model missing from `[effort_levels]`,
was dispatched unchecked. `load_task` still does not check it, because it
also runs for tasks already in flight: when a restarted supervisor adopts a
running worker, and in the silent-orphan reaper. Rejecting a task there
would break a worker that has already started.

The pair is now checked with `runner.effort_levels.validate_effort`
wherever the runner decides to dispatch, against the settings it runs with:

- **The supervisor's candidate selector** checks every task it would
  otherwise dispatch. A task that fails is parked like an ADR-0030 readiness
  hold: status `deferred`, `deferred_reason` set to `invalid effort: ` plus
  the error, no `next_eligible_at`, and one WARNING when it is parked. The
  state is written only when the status or reason changes. No attempt or run
  is recorded, so the circuit breaker is not involved. On the first tick
  after the YAML is fixed, or after `[effort_levels]` is fixed and the
  supervisor re-reads it on SIGHUP, the task goes back to `pending`. Only a
  hold with that prefix is cleared, never an operator's park, a hook
  deferral or a readiness hold. The check runs after the status filter, so
  a completed, running or circuit-broken task is never re-parked, and before
  `depends_on`, so an authoring error surfaces as soon as the task is
  queued.
- **The dispatch thread** (`_dispatch_one_safely`) re-checks as the
  backstop for every path that spawns it.
- **Force-dispatch** refuses the task on every path, because force
  overrides the throttle, not the task's configuration. The CLI exits 2
  before it dispatches or writes a request, and the supervisor drops a
  request that is already written.
- **`doctor`'s `task_yamls` check** FAILs on such a task, and each
  **`queue list`** row carries `effort_error`, which is `null` when the pair
  is accepted.

A pair is either accepted or rejected, with no alias or case folding. The
packaged `[effort_levels]` keeps previous-generation models so that queued
tasks naming them keep dispatching.
