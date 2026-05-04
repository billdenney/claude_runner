# ADR-0013: Pre/post-dispatch shell hooks (replaces built-in worktree mgmt)

- **Date:** 2026-05-03
- **Status:** accepted

## Context

The existing runner has a project-specific `tracking/sync_worktrees.py`
script that runs before each batch to fast-forward worktrees to
`origin/main`. Other projects using the runner have different
pre-dispatch needs (compilation, fixture refresh, environment setup).

## Decision

The runner exposes generic pre/post-dispatch shell hooks, configured per
queue:

```toml
[hooks]
pre_dispatch_command  = "python tracking/sync_worktrees.py --task=$TASK_ID"
pre_dispatch_timeout_s = 120
post_dispatch_command = ""
```

Environment variables exposed: `$TASK_ID`, `$TASK_WORKING_DIR`,
`$TASK_MODEL`, `$ATTEMPT`, `$SESSION_ID`. Pre-dispatch hook failure
(non-zero exit) aborts dispatch for that task. Post-dispatch hook
failure logs a warning but doesn't fail the task.

## Alternatives considered

- **Built-in worktree management:** rejected; couples the runner to git +
  R workflows; useless for non-git projects.
- **Plugin entry points:** heavier to implement and document; shell
  hooks are sufficient and familiar.

## Consequences

- (+) Project-specific pre/post-dispatch logic without code changes.
- (+) Familiar interface (shell command + env vars).
- (-) Operators must implement their own scripts (mitigated: existing
  `tracking/sync_worktrees.py` carries over verbatim).
- (-) Hook failure modes are stringly-typed.

## Reversibility

High. Operators set `pre_dispatch_command = ""` to disable hooks
entirely.
