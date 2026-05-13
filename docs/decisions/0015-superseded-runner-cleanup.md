# ADR-0015: Cleanup after the superseded `claude_runner` audit

- **Date:** 2026-05-13
- **Status:** accepted

## Context

ADR-0001 chose a full rewrite over wrapping the older `claude_runner` CLI.
The superseded source is preserved on this repo's history — `git show
aff0d34^2:src/claude_runner/<file>` recovers the old layout from before
the rewrite was merged into `origin/main`.

A side-by-side audit was run against:

- the superseded `claude_runner` package layout, and
- the production `claude_runner.toml` last deployed at
  `mab_human_consensus/claude_runner_queue/`.

The audit identified items that either survived as dead code in the new
package or are gaps the operator has decided not to port.

## Decisions

1. **`Task.inject_preamble` field is removed.**
   The Task model in `queue/schema.py` declared
   `inject_preamble: bool = True`; no dispatch code consumed it. A
   filesystem-wide check showed no global preamble mechanism (no
   user-level CLAUDE.md content, no project CLAUDE.md content, no
   per-worktree CLAUDE.md content, no settings.json hook, no
   auto-loaded skill) supplies the sidecar protocol / worktree /
   env-var instructions that the superseded `runner/preamble.py`
   prepended to every prompt. The field was vestigial. Removed
   alongside its bullet in
   `skills/runner-add-task/SKILL.md`.

2. **First-time queue setup is documented at
   `docs/first-time-setup.md`.** Replaces the deleted reference to a
   nonexistent `claude-task-runner config init` subcommand in
   `README.md`. The new doc walks an operator from an empty directory
   to a running supervisor draining its first task, including the
   minimal `claude_runner.toml` template.

3. **Multi-source budget fallback is not supported.**
   The superseded code had `ccusage`, `context_cmd`, `api_headers`,
   and `static` sources behind a `budget_source` selector. Operator
   experience was that the alternatives did not work reliably, so the
   rewrite ships only `ClaudeUsageSource` (`usage/source.py`). The
   absence of a fallback is intentional. `claude-task-runner doctor`
   exercises the single source.

4. **Multi-threshold rolling-window circuit breaker is not supported.**
   The superseded code consumed four keys
   (`max_consecutive_failures`, `failure_rate_threshold`,
   `failure_rolling_window`, `failure_rate_min_samples`). The
   rewrite exposes only `failure_circuit_breaker_threshold` (single
   consecutive count) under `[failure_classifier]`. Simpler model,
   fewer interaction effects.

5. **`backend` selector is not supported.**
   Only the subprocess backend is shipped (ADR-0001). The asyncio
   backend was incompatible with `--thinking` in claude CLI 2.1.85 and
   was deliberately dropped.

6. **Worktree templating is the hook's job, not the runner's.**
   The superseded TOML used
   `worktree_root = ".../${task_id}"`; ADR-0013 delegates worktree
   creation to pre-dispatch shell hooks, which receive `$TASK_ID`
   via `HookEnv`. The runner uses `task.working_dir` verbatim; the
   operator's hook (or the queue-build tool) resolves any template
   substitution before that value lands in the YAML.

## Items the audit found but the operator did not request

A sweep over every field declared on the `Task` model surfaced
`inject_preamble` as the only one with zero references in `src/` and
zero references in `tests/`. The remaining sixteen fields all carry
their weight (lowest non-zero count: `force_dispatch_in_eow` and
`weekly_deferrable` at 1 and 2 src-refs respectively, with at least one
test ref each — alive).

## Reversibility

Each item is low-cost to reverse:

- **`inject_preamble`**: re-declare the field, then wire it from
  `runner/dispatcher.py` to prepend a configurable preamble.
- **`config init` subcommand**: add a `config_cmd.py` under `cli/`
  that materialises a `claude_runner.toml` template.
- **Multi-source budgets, multi-threshold circuit breaker,
  asyncio backend, worktree templating**: each becomes an ADR of its
  own once an operator brings a concrete need.

## Notes

The `mab_human_consensus` deployment's literature corpus + active
queue was consolidated into a new ingestion repo as part of the same
work cycle. None of the new files live in `claude_task_runner`; this
ADR records the cleanup that touches this package's source only.
