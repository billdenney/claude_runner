# ADR-0034: Reclaim the worktrees of finished tasks

- **Date:** 2026-09-25
- **Status:** accepted
- **Related:** ADR-0013 (worktree creation belongs to the pre-dispatch hook),
  ADR-0020 and ADR-0032 (a worktree or a local branch can hold the only copy
  of a task's work), ADR-0023 (`working_dir` conventions), ADR-0025 (dispatch
  threads outlive the `completed` write).

## Context

ADR-0013 moved worktree management out of the runner: a queue's pre-dispatch
hook creates one git worktree per task, and the runner only passes
`$TASK_WORKING_DIR`. Nothing ever removes one. That was deliberate. ADR-0020
and ADR-0032 exist because `completed` has repeatedly hidden work that lived
nowhere else: uncommitted files in a worktree, or commits on a branch that was
never pushed. A cleanup that trusts `completed` alone would destroy exactly the
work those gates were written to protect.

The cost of never cleaning up arrived on 2026-09-25. The nlmixr2lib ingestion
queue had **305 worktrees holding 36 GB** (about 120 MB and 5,400 files each).
**244** of them belonged to tasks that were all three of these:

- `completed` in their state file;
- on a branch that was already an ancestor of `origin/main`, because the
  runner-merge-claude-branches consolidation had folded it in with a real
  merge commit;
- clean in `git status`, apart from testthat failure snapshots under
  `tests/testthat/_problems/`.

A one-off script in the queue repository reclaimed them
(`_scripts/reclaim_task_worktrees.sh`). Its header says it becomes obsolete
"when claude_task_runner reclaims completed tasks' worktrees itself". A script
that nothing runs rots, and a queue-local script with hard-coded paths does not
help the next queue. The runner is where task status lives, so the runner
should own the decision.

## Decision

Add `claude-task-runner worktree reclaim --queue <dir> [--apply]` (a dry run
by default), plus an opt-in periodic supervisor pass, both configured by a new
`[worktree_reclaim]` table. A task's worktree is removed only when **all** of
these hold:

1. **The task is finished.** Its state file says `completed`. Every other
   status keeps its worktree, enumerated from the schema in the tests:
   `awaiting_sidecar`, `running`, `failed`, `deferred`, `pending`,
   `possibly_hung`, `failed_circuit_breaker` and `weekly_paused`, plus a
   missing or unreadable state file. `claude --resume`, a retry and an
   answered sidecar all need the directory.
2. **Its commits are on the parent branch.** The worktree has the task's
   branch checked out (`branch_template`, default `claude/{task_id}`), and
   that branch is an ancestor of `<remote>/<parent_branch>` right after a
   fetch of that one ref. "Pushed" is not enough, because consolidation
   housekeeping deletes the pushed task branches. Reachability from the parent
   branch is the only durable proof.
3. **Nothing uncommitted would be lost.** `git status --porcelain` is empty,
   except for untracked paths listed in `discardable_untracked` (default
   `tests/testthat/_problems/`). Only those force `git worktree remove
   --force`. A modified or staged file under the same prefix still keeps the
   worktree.

The following guards come on top. Each one only makes the reclaim more
conservative:

- **Not in flight.** The dispatcher writes `completed` *before* it runs the
  post-dispatch hook inside the worktree. The supervisor pass skips its live
  slot set. The CLI skips the in-flight set in `supervisor.json`, and refuses
  to run if that file is unreadable.
- **Only linked worktrees.** A repository's main worktree, a bare repository,
  or a checkout git does not list as a linked worktree is never removed. The
  same holds for a worktree someone locked with `git worktree lock`.
- **One owner.** A working_dir named by more than one task YAML is kept.
- **The expected branch.** A worktree on another branch or a detached HEAD is
  kept. The merge check would otherwise test the wrong commits.
- **Declared output.** A `deliverable_paths` entry inside the worktree that git
  ignores keeps it. Ignored files are otherwise treated as disposable, the same
  way `git worktree remove` treats them.
- **Re-check before acting.** Right before each removal, the task status and
  `git status` are read again. That happens under the pre-dispatch hook's
  `flock` when `lock_file` is set, which is also held around the fetch. A
  concurrent `git worktree add` then waits instead of failing on git's
  repository locks, which the hook would count as a hard failure.
- **git's own safety nets stay armed.** The branch is deleted with
  `git branch -d`, never `-D`. When git refuses, the branch is kept and
  reported. `--force` is passed once, so a locked worktree still refuses.
  There is no `git worktree prune`, and remote branches are never touched.

Candidates come from the task YAMLs in `todo/`, the same source the
orchestrator dispatches from. Worktrees that no task names are never
considered.

The periodic pass is off by default (`periodic = false`). When enabled it
runs on the supervisor's first tick and then every `interval_s`, skips drain
mode, and attempts at most `max_per_pass` removals. It runs synchronously
after `tick_dispatch`, like the steady-state reaper. Each removal becomes a
`worktree_reclaimed` event, and each failure a warning notification plus a
`worktree_reclaim_failed` event.

## Alternatives considered

- **Keep the queue's shell script.** It hard-codes one queue's paths, nothing
  runs it, and it re-derives the task status by parsing YAML with `awk`.
- **Remove the worktree when the task completes.** That is too early. The
  branch is not merged until a consolidation runs, days later, and ADR-0032
  shows a completed branch can still carry unpushed work.
- **Require "pushed" instead of "merged".** The consolidation deletes the
  pushed branches afterwards, so a pushed-but-unmerged branch has no durable
  copy.
- **`git worktree prune` and `git branch -D`.** Prune edits the registry
  entries of worktrees this pass never verified, and `-D` overrides the one
  independent check git offers.
- **Count ignored files as work (`git status --ignored`).** R build output,
  `.Rcheck` directories and object files would then keep nearly every
  worktree. Declared deliverables are the one exception that matters.
- **Run the periodic pass on a background thread.** That would let it race
  the dispatch loop it shares a queue with. A bounded synchronous pass is
  simpler to reason about, and the CLI handles a large backlog in one go.

## Consequences

- (+) Disk use is bounded by the tasks that are actually unfinished or
  unmerged, and the queue's one-off script can be deleted.
- (+) Every kept worktree is reported with the condition it failed, so the
  dry run doubles as an audit of stranded work.
- (−) An enabled periodic pass fetches `<remote>/<parent_branch>` once per
  repository per pass, using the supervisor's environment and credentials,
  as the pre-dispatch hook already does.
- (−) A pass blocks the supervisor loop while it removes up to `max_per_pass`
  worktrees, which takes a few seconds each for a large tree. Clear a backlog
  with the CLI instead. Every pass also reads every task YAML in `todo/`, the
  same scan the orchestrator makes each tick. On the 5,294-task nlmixr2lib
  queue that scan took 12.5 s with PyYAML's pure-Python loader, and 1.6 s
  once the queue store switched to LibYAML's `CSafeLoader` (both measured
  2026-09-25). The git work of a dry run took about one second.
- (−) A task whose YAML has left `todo/` (for example, moved to a `done/`
  directory by hand) is invisible to the runner, and so is its worktree.
  Reclaim before moving YAMLs, or remove those worktrees by hand.
- The dry run still fetches, which updates that one remote-tracking ref.
- The default allow-list is testthat's snapshot directory. It never matches in
  a repository without that path. Set `discardable_untracked = []` to require
  a spotless `git status`.
- An operator who resets a reclaimed task to `pending` loses nothing. The
  hook recreates the worktree from `origin/main`, which already contains the
  deleted branch.

## Reversibility

High. `periodic = false` (the default) plus not running the CLI restores the
previous behavior exactly. Each removal is lossless by construction: every
commit is on the remote's parent branch, and nothing uncommitted is discarded
except the allow-listed untracked paths.
