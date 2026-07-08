# 0030 — Mechanical readiness gates (`Task.requires`)

Status: Accepted (2026-07-08)

- **Related:** ADR-0026 (exit-1 deferral → `deferred`), ADR-0029 (deferral
  slot-leak fix + `dispatch_block_file`), ADR-0013 (pre/post-dispatch hooks),
  ADR-0020 (output-evidence gate).

## Context

A task can be blocked on a purely **mechanical** precondition — one a script
can answer with no AI/worker involvement. The two that occur in practice are
"does this input file exist yet?" and "has the operator written the sidecar
response file?". Today these two are handled asymmetrically:

- **Sidecar response — ideal.** The supervisor's candidate selector
  (`_eligible_candidates`) checks `list_open_sidecars` every tick: a pure
  "does `response-NNN.json` exist" filesystem probe. A task with an
  unanswered request is simply not selected, and it is re-admitted the first
  tick after the response appears. No dispatch, no worker, no cooldown.

- **File wait — not.** A task waiting on a file is **dispatched**: a thread +
  in-flight slot spawn and run the pre-dispatch hook, which `exit 1`s to
  defer (ADR-0026). It burns no AI tokens (the hook never spawns `claude`),
  but it *does* consume a dispatch cycle and briefly an in-flight slot, and —
  because a deferral re-checks no sooner than
  `[failure_classifier].deferral_recheck_cooldown_s` (~15 min) — a file that
  arrives at second 1 is not noticed for up to that cooldown. ADR-0029 made
  each such defer *harmless to the slot*, but it is still a dispatch the
  supervisor could have avoided, and the unblock is cooldown-laggy.

The file wait should behave like the sidecar wait: evaluated by the
supervisor's selector, mechanically, every tick, unblocking the instant the
element appears — and this should generalise to any no-AI precondition, so a
question a script can answer never costs a dispatch.

## Decision

Add a declarative, mechanically-evaluated readiness gate to the task model.

- **`Task.requires: list[ReadinessRequirement]`** (default `[]`, backward
  compatible). Each element is a typed, cheap-to-check precondition:
  - `{kind: "file", path: <rel-or-abs>}` — the path must exist (relative
    paths resolve against the queue dir).
  - `{kind: "sidecar_response"}` — every sidecar request the task filed has a
    response (i.e. the task is not in the open-sidecar set). This folds the
    already-good sidecar behaviour into the same framework.
  - `note` (optional) — operator-facing description surfaced when reporting
    why a task waits.

- **Evaluation in the selector, never a dispatch.**
  `runner.readiness.unmet_requirements(task, queue_dir)` returns the unmet
  elements using only `Path.exists()` probes and a set lookup. It is pure,
  side-effect-free, and cheap enough to run for every candidate on every
  tick. `_eligible_candidates` calls it after the `depends_on` check and
  drops any task with unmet requirements from the candidate set — so the task
  is never dispatched while blocked, and is admitted the first tick after all
  elements are satisfied (poll cadence, not the deferral cooldown).

- **Extensible by design.** A new gate type is two edits: extend the
  `ReadinessKind` Literal and add a branch to `unmet_requirements`. The
  scaling contract is "safe to run for every task every tick," so branches
  must stay cheap and in-process (an unknown kind fails **closed** — it
  blocks and logs, never silently passes).

The pre-dispatch hook stays as the enforcing backstop: it still creates the
worktree and can still `exit 1`-defer for reasons not expressible
declaratively. `requires` is the fast path that keeps a declared, mechanical
wait off the dispatch machinery entirely.

## Alternatives considered

- **A per-task "readiness hook" the supervisor runs each tick.** Maximally
  general (any shell predicate), but a subprocess per task per tick does not
  scale — this queue has ~2.4k tasks; a fork+exec sweep every poll interval
  is untenable. Declarative gates checked in-process cost a `stat()` each.
  A `{kind: "command"}` type could be added later for the rare task that
  needs it, explicitly caveated as heavier; it is deliberately **not** in the
  core set.
- **Derive requirements from `needs_acquisition.jsonl` `target_path` rows.**
  Those rows are keyed by file path, not task id; mapping a missing file back
  to the tasks that need it requires the queue's `task_inputs.py` logic, which
  the runner does not (and should not) embody. A task declaring its own
  `requires` is the clean inverse.
- **Leave it hook-only (post-ADR-0029).** The defer is already harmless to
  the slot, but it still spends a dispatch cycle per cooldown and unblocks
  laggily. Bringing the file case to sidecar-parity is the point.

## Consequences

- A file-blocked task that declares `requires` never dispatches while
  blocked and unblocks within one poll interval of the file appearing —
  eliminating both the wasted dispatch cycle and the cooldown lag for that
  class of wait.
- New schema: `ReadinessRequirement` + `Task.requires`. Additive and
  defaulted, so existing task YAMLs and the persisted state load unchanged;
  no migration.
- **Adoption is a queue concern.** The runner ships the mechanism; a queue
  benefits only once its tasks populate `requires` (e.g. the nlmixr2lib
  queue's task-creation tooling emitting `{kind:"file", path:"<stem>_trimmed.md"}`
  per input, plus a one-time backfill for existing tasks). Until then the
  hook's file-check continues to handle those tasks (harmlessly, per
  ADR-0029). The hook's file-existence `exit 1` becomes redundant for any
  migrated task and can be dropped from the hook later.
- Operator visibility: `unmet_requirements` is a pure function a CLI/doctor
  surface can call to answer "why isn't this task dispatching?" (a
  `queue why-blocked` command is the natural follow-up; not built here).

## Reversibility

High. The gate is additive: a task with no `requires` behaves exactly as
before. Reverting means deleting the field + the one selector call; no
persisted state depends on it.
