# ADR-0023: `[queue].working_dir_template` for `queue add`

- **Date:** 2026-05-29
- **Status:** proposed
- **Related:** ADR-0013 (pre/post-dispatch hooks), ADR-0014 (every cutoff
  is a setting), ADR-0020 (gate `completed` on output artifact)

## Context

The `claude-task-runner queue add` CLI writes new Task YAMLs with
`working_dir: null` and provides no way to populate the field at write
time. For queues whose pre-dispatch hook depends on `working_dir`
(ADR-0013 documents the env-var contract — the hook receives
`$TASK_WORKING_DIR`), this is a footgun:

1. The operator runs `queue add --id 130-foo ...`.
2. The new YAML carries `working_dir: null`.
3. The supervisor dispatches the task; the pre-dispatch hook receives
   `$TASK_WORKING_DIR=""`.
4. The hook short-circuits ("nothing to set up") and exits 0.
5. The agent runs in the queue dir rather than the intended per-task
   worktree, fails for reasons unrelated to the task itself (the
   queue dir is not a git repo; the task-specific skill is invisible),
   and either sidecars or burns through the failure circuit breaker.

In the live `nlmixr2lib_ingestion` queue this has recurred at least
three times — tasks `130-lowe_2009_omalizumab`,
`131-laffont_2025_nalmefene_naloxone`, and
`132-mann_2022_translational_model` each needed manual YAML edits to
populate `working_dir` after the operator forgot at `queue add` time.

The convention is regular: every task in the queue maps to a worktree
at `/home/bill/github/nlmixr2/nlmixr2lib/.claude/worktrees/<task_id>`.
Encoding the convention once in the per-queue config makes the regular
case zero-effort and keeps `queue add` invocations short.

Some tasks legitimately have no working_dir (e.g. the queue's
DDMORE-categorization shards, which don't operate on a single paper's
checkout). Whatever solution we adopt must remain a *useful default*,
not a forced value.

## Decision

Add a `[queue]` block to per-queue `claude_runner.toml` (and the
package defaults) with one field:

```toml
[queue]
working_dir_template = "/home/bill/github/nlmixr2/nlmixr2lib/.claude/worktrees/{task_id}"
```

The template supports a single `{task_id}` substitution (Pythonic
`str.format(task_id=...)`). Unknown placeholders raise an error at
template-application time so typos surface fast.

`queue add` resolves the task's `working_dir` with this precedence:

1. `--no-working-dir` flag — forces `null` regardless of template.
2. `--working-dir <path>` flag — explicit value wins over template.
3. `[queue].working_dir_template` (if non-empty) — substitute
   `{task_id}` and use.
4. None — preserves the historical behavior (`working_dir: null`).

Defaults to empty string. Existing queues whose `claude_runner.toml`
pre-dates this section keep working unchanged.

A `doctor` battery item (`working_dir_template`) emits **WARN** when
the combination is a near-certain dispatch failure: a pre-dispatch
hook is configured, the template is unset, AND at least one pending
task in `todo/` has `working_dir: null`. The remediation points at the
new `queue backfill-working-dir` subcommand.

`claude-task-runner queue backfill-working-dir --queue <dir>` walks
`todo/`, applies the template to any task whose `working_dir` is
already null, and rewrites the YAMLs atomically. Idempotent: tasks
that already have `working_dir` set are skipped. A `--dry-run` flag
reports what would change without writing.

## Alternatives considered

- **Make `working_dir` mandatory at `queue add` time.** Rejected.
  Breaks queues that legitimately have null-working_dir tasks
  (categorization shards); shifts the footgun from "operator forgets"
  to "operator must remember per-task whether to type the flag" — same
  failure mode in a different costume.
- **Schema-level default-via-validator** (e.g. populate `working_dir`
  from a class-level template inside `Task.model_validate`). Rejected.
  Couples the schema to deployment-specific paths; tasks would change
  meaning when re-loaded by a different queue's config; "this task
  YAML is self-describing" stops being true.
- **Per-queue Jinja/template engine.** Rejected. One substitution
  (`{task_id}`) covers every case we have today; pulling in a
  templating dependency for a single placeholder is overkill, and the
  failure modes are subtler.
- **Bake the hook itself smart enough to derive the worktree path
  from `$TASK_ID`.** Rejected as out of scope for the runner — the
  hook is per-queue project-specific (ADR-0013) and the *intent* of
  the task YAML is what the runner publishes via env vars. The runner
  shouldn't ask hooks to second-guess missing fields; it should let
  operators populate the field cleanly.

## Consequences

- (+) `queue add` becomes one-flag-fewer for the common case in queues
  that have a worktree convention.
- (+) Operators stop hand-patching YAMLs after the fact — the
  doctor check surfaces the misconfiguration before dispatch.
- (+) Existing queues that don't set the template see no change in
  behavior.
- (-) One more setting to learn. Mitigated by the doctor check that
  flags the missing-template case only when it's a probable bug
  (hook + null `working_dir`).
- (-) The backfill helper writes to the operator's `todo/` YAMLs. It
  is idempotent and refuses to run when no template is configured,
  but it does mutate files on disk — operators should commit their
  queue dir before running it on large batches.

## Reversibility

High. Operators set `working_dir_template = ""` (or remove the
`[queue]` block) to revert to the historical behavior. The new flags
(`--working-dir` / `--no-working-dir`) and the doctor check and
backfill subcommand are additive — they don't disturb existing
workflows when the template is unset.
