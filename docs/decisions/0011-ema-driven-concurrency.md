# ADR-0011: EMA-driven concurrency tuning (re-enabled)

- **Date:** 2026-05-03
- **Status:** deprecated; never implemented (see the 2026-09-25 update)

## Context

The previous runner had EMA-based concurrency tuning that was disabled
because the bash `claude-usage` source produced inaccurate utilization
percentages — predictions were noisy, dispatch decisions were wrong,
operator turned EMA off. Concurrency was pinned to 1.

Now that we have a parsed-and-validated `/usage` source with drift
detection, EMA prediction becomes trustworthy again.

## Decision

Re-enable EMA-driven concurrency:

- Each task has a `task_type = (model, effort, allowed_tool_set_hash)`.
- After each completed run, update the EMA for that task_type:
  `ema = alpha * observed + (1 - alpha) * ema`.
- At dispatch time:
  `predicted_pct = (used + sum(in_flight_estimates) + ema.predict(new_task.task_type)) / budget`.
- Cold-start (no samples for this task_type): use a TOML-configured prior.
  Once at least one sample exists but fewer than `prior_warmup_samples`,
  the prediction blends prior and observed EMA in proportion to sample
  count: with `w = sample_count / prior_warmup_samples`, predict
  `w * observed + (1 - w) * prior`. After `prior_warmup_samples` real
  samples the prior is dropped entirely. (See `runner/ema.py`.)

EMA values persist to `<queue>/.claude_task_runner/ema.json`.

## Alternatives considered

- **Static `max_concurrency = 1`:** safe but wastes budget when tasks
  are short.
- **Token-bucket without per-task-type EMA:** loses the predictive power;
  treats all tasks identically.
- **Server-side query for in-flight cost:** not exposed by the Anthropic
  API.

## Consequences

- (+) Higher utilization without crashing — concurrency is tuned per task
  size.
- (+) Cold-start is safe via priors.
- (-) Adds an EMA file to per-queue state.
- (-) Misclassification of task_type (e.g., very different prompts in the
  same `(model, effort, tools)` bucket) would yield bad predictions.
  Mitigated by the safety net of the 90% no-dispatch threshold.

## Reversibility

High. Setting `[concurrency].max_concurrency = 1` and ignoring the EMA
predictions is functionally equivalent to disabling EMA.

## Update (2026-09-25)

This decision was never implemented, and its code has been removed.
`runner/ema.py` held the EMA and the blended predictions and was
unit-tested, but no production code ever called `update_bucket`, so no
queue ever wrote an `ema.json`. The dispatch-time formula above was not
wired either. The predictions' only caller, `runner/runtime_stats.py`,
was an end-of-week-push fitness check that nothing called, and ADR-0022
removed the end-of-week push. The formula's denominator, the plan token
budgets (`[claude].plan` and `[plans.*]`), was never read.

Removed: the `[ema]` settings table (`alpha`, `prior_warmup_samples`,
`runtime_p90_multiplier` and the `[ema.priors.*]` tables),
`runner/ema.py`, `runner/runtime_stats.py` and the doctor's `ema` check.
The loader rejects a queue TOML that still has an `[ema]` table, with a
message saying to delete it. Deleting it changes nothing.

Concurrency comes from `[concurrency]`: `initial_concurrency` until a
first task completes in the queue, then `max_concurrency`. Each account's
`runner-account.toml` caps its own share with `max_concurrency`. The
ADR-0022 `[dispatch_pct.*]` policy throttles all of it from the
utilization `/usage` reports.
