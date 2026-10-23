# ADR-0036: A `depends_on` id that names no task is a loud hold, not a wait

- **Date:** 2026-10-23
- **Status:** accepted
- **Related:** ADR-0010 (the effort gate, whose park this reuses), ADR-0030
  (readiness gates, the first selector park)

## Context

The selector held a task while any id in its `depends_on` was not in the
set of completed tasks. It could not tell a dependency that exists and has
not finished from one that does not exist at all, so it counted both under
`depends_on`. `queue holds`, the supervisor's `no eligible tasks:` line and
`/runner-status` ("waits for another task to finish") all reported a task
waiting for a task that will never run as if it were waiting normally.

On the nlmixr2lib queue three tasks depended on
`115-pouzin_2022_tusamitamab`, `122-yao_2018_guselkumab` and
`129-ng_2009_clozapine`. None of the three had a task file or a state file.
Two of the waiting tasks had never been dispatched in months, and nothing
said why.

## Decision

The selector tells the two apart. A dependency exists, and may still
complete, when any of these holds:

- its YAML is in `todo/` (the stem of a file the selector already lists),
  in any state, parseable or not: only tasks in `todo/` are dispatched;
- its state is `completed`, wherever its YAML is (some queues move a
  finished task's YAML to `done/`, which the runner does not read);
- its state is `running` or `possibly_hung`: a worker may still finish it;
- its state file does not parse, so nothing can be told from it.

The state-file cases come from `_state_task_ids`, the one pass over the
state files that already found the completed ids, so no directory is
listed or file parsed beyond what each tick already did. Any other state
with no `todo/` YAML (`pending`, `failed`, `deferred`, `awaiting_sidecar`,
`failed_circuit_breaker`, `weekly_paused`) is a task that is never
dispatched again, such as one whose YAML was renamed or deleted, so a
dependency on it is missing.

A task whose `depends_on` names an id outside that set is held under a new
category, `depends_on_missing`. A task with both a missing and an
unfinished dependency is held there too, since waiting cannot release it.
The check runs where the `depends_on` check ran: after the status filter,
so a completed, running or circuit-broken task is never parked for it, and
after the effort gate, so an invalid effort is reported first.

The task is parked as the effort gate parks one (ADR-0010): status
`deferred`, `deferred_reason` set to `missing dependency: depends_on names
<ids>, which has no task file in todo/ and is not completed or running` (or
"which have ... and are ..."), no `next_eligible_at`, and no attempt or run
recorded; the session, attempt count and runs are kept. The supervisor logs one
WARNING when it parks the task, naming the task and the missing ids and
saying to fix the task's `depends_on` or create the missing task. The state
is written only when the status or reason changes, so a queue that keeps a
task parked logs and writes once, and a change in the missing ids is a new
reason with a new WARNING. On the first tick after the ids exist, or leave
`depends_on`, the task goes back to `pending`, and then waits under
`depends_on` if a dependency exists but has not completed. Only a hold with
the `missing dependency: ` prefix is cleared this way.

A read-only selection (`preview_selection`, used by `queue holds`) reports
the same category and writes and logs nothing. `queue holds --json` lists
each such task under `missing_dependencies` with its `missing` ids, and
`/runner-status` lists them under "Not normal right now".

`queue force-dispatch` does not check `depends_on`, and `deferred` is a
status it accepts, so it still starts such a task on request.

## Alternatives considered

- **A skip category with an in-memory set of tasks already warned about.**
  It writes nothing, but the set is new state that the supervisor would
  have to keep across ticks and prune as tasks go, it would warn again
  after every restart, and `queue states` and `queue show` would still show
  the task as `pending` with no reason. That silence is what
  `_park_deferred` was written to end for the readiness and effort gates,
  and it is how this hold went unnoticed for months.
- **Counting any state file as proof that the task exists.** That leaves
  the case this ADR is for silent whenever a state file is left behind: a
  task whose YAML was renamed (as the nlmixr2lib queue renames tasks to
  generic drug names) or deleted keeps its `pending`, `failed` or
  `deferred` state, and the selector itself writes a state for a task it
  parks before it ever runs. None of these is dispatched again.
- **Rejecting the task when it loads.** `load_task` reads one file and
  cannot see the rest of the queue, and the dependency may be created
  later. The hold has to be re-checked each tick.
- **Treating a missing dependency as met.** That dispatches a task before
  work it was told to wait for, which may simply not have been queued yet.

## Consequences

- A missing dependency is visible in the supervisor log, `queue holds`,
  the idle line, `queue states`, `queue show` and `/runner-status`.
- The first park of a task that has never run writes a state file for it,
  as the readiness and effort gates already do.
- A dependency whose YAML is in `todo/` but which will not run again, such
  as one in `failed_circuit_breaker`, still holds its dependents under
  `depends_on`.
- As with the effort and readiness gates, the park and un-park return a
  `failed` task, or an `awaiting_sidecar` one whose questions are answered,
  as `pending`, not as the status it had.
- As with those gates, the park replaces a `deferred_reason` set by hand
  without a `next_eligible_at`, since such a reason does not hold the task.
- As with those gates, a park whose state write fails is retried, and its
  WARNING logged again, on every tick until the write succeeds.

## Reversibility

Low. Removing the gate returns such tasks to `depends_on`. A task still
parked with the `missing dependency: ` prefix would keep that stale reason
until it next runs, but would not be held by it: a `deferred` state without
`next_eligible_at` passes the status filter, and the selector's other
checks decide.

## Update (2026-10-24): dependencies that exist but cannot complete

Three kinds of dependency still held their dependents silently under
`depends_on`, though each has a task YAML in `todo/` and so exists:

- the task's own id;
- a task in a dependency cycle with it (A depends on B, B on A, or longer);
- a task in `failed_circuit_breaker`, which the runner does not dispatch
  again until an operator resets its state.

None of these completes while nothing changes, so each is now held as a new
category, `depends_on_unmeetable`, by the same mechanism as a missing
dependency: parked `deferred` with `unmeetable dependency: depends_on names
<id> (<why>); ...`, where `<why>` is `the task itself`, `in a dependency
cycle among <the cycle's first three tasks, sorted> [and N more]` or
`failed_circuit_breaker`;
one WARNING per transition; un-parked the first tick after the depends_on
is fixed, the cycle broken or the stopped task reset, and only from a hold
with that prefix; nothing written or logged by a read-only selection.
`queue holds --json` lists each such task under `unmeetable_dependencies`,
each dependency with its `id`, `cause` (`self`, `cycle`,
`failed_circuit_breaker`) and, for a cycle, the index of its cycle in
`dependency_cycles`, which lists every task of each cycle once; and
`/runner-status` lists them under "Not normal right now". A reason, the
WARNING, the text output and the status report name a cycle by three of its
tasks and how many more, so that none of them, nor the JSON, grows with the
square of a cycle's length.

The cycles are found once per selector pass, not per task: the selector
reads every task YAML before classifying any (it read each once anyway),
and Tarjan's strongly connected components algorithm runs over the graph of
the tasks and their dependencies on one another, in time linear in tasks
plus dependencies, iteratively so a long chain cannot exhaust the recursion
limit. A completed task, or one a worker may be running (`running`,
`possibly_hung`, or an unreadable state), is no node: depending on it is
met, or may yet be. The circuit-broken ids come from the one pass over the state
files that already finds the completed ones. No directory is listed or file
parsed beyond what each tick already did.

Every task in a cycle is held, each naming the member it depends on. A task
that only depends on a member of a cycle, or on any task held by its own
gate, waits under `depends_on`: one WARNING per cause, and fixing that
cause releases it. A missing dependency is reported before an unmeetable
one, and an unmeetable one before an unfinished one.

It is its own category rather than a second meaning of
`depends_on_missing` because the fixes differ (fix an id or create a task;
break a cycle or reset a stopped task), and because the details differ: a
missing dependency is an id, an unmeetable one an id and a cause. Sharing
the category would make `missing_dependencies` and its count mean two
things, and the operator would have to read every reason to tell a typo
from a loop.

This replaces the consequence above that a `failed_circuit_breaker`
dependency with its YAML in `todo/` holds its dependents under
`depends_on`. A dependency whose own hold is reported (an unparseable YAML,
an invalid effort, the block list) still holds its dependents there.
