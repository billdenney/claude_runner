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

The selector tells the two apart. The tasks that exist are the stems of the
`todo/*.yaml` files the selector already lists, the stems of the state
files `_state_task_ids` already reads to find the completed ones, and the
completed ids themselves. A state file or a task file counts whether or not
it parses. No directory is listed beyond the two the selector already reads
every tick.

A task whose `depends_on` names an id outside that set is held under a new
category, `depends_on_missing`. A task with both a missing and an
unfinished dependency is held there too, since waiting cannot release it.
The check runs where the `depends_on` check ran: after the status filter,
so a completed, running or circuit-broken task is never parked for it, and
after the effort gate, so an invalid effort is reported first.

The task is parked as the effort gate parks one (ADR-0010): status
`deferred`, `deferred_reason` set to `missing dependency: depends_on names
<ids>, but no task in todo/ or state/ has that id` (or "those ids"), no
`next_eligible_at`, and no attempt or run recorded. The supervisor logs one
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
- A dependency that exists but can never complete, such as one in
  `failed_circuit_breaker`, still holds its dependents under `depends_on`.

## Reversibility

Low. Removing the gate returns such tasks to `depends_on`. A task still
parked with the `missing dependency: ` prefix would keep that stale reason
until it next runs, but would not be held by it: a `deferred` state without
`next_eligible_at` passes the status filter, and the selector's other
checks decide.
