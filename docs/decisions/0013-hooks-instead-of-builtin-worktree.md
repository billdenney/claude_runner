# ADR-0013: Pre/post-dispatch shell hooks (replaces built-in worktree mgmt)

- **Date:** 2026-05-03
- **Status:** accepted
- **Refined by:** ADR-0023 (working_dir contract clarification)

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

Hooks should treat `$TASK_WORKING_DIR=""` (the task has
`working_dir: null` in its YAML) as "no per-task working dir needed"
and exit 0 without erroring — some tasks legitimately don't have a
working_dir (e.g. categorization shards that don't operate on a single
per-task checkout). The nlmixr2lib popPK ingestion hook
(`_scripts/hooks/setup_worktree.sh`) is the reference implementation
of this contract. Operators whose hook REQUIRES `working_dir` should
configure `[queue].working_dir_template` (ADR-0023) so `queue add`
populates the field automatically rather than relying on operators to
remember the flag.

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
