# ADR-0025: Restart-survivable workers (worker adoption)

- **Date:** 2026-06-13
- **Status:** accepted
- **Related:** ADR-0020 (output-evidence gate), ADR-0024 (multi-account
  session affinity); PR #55/#57/#59 (silent-orphan reaper, three-layer
  heartbeat); supersedes the graceful-drain ExecStop wiring from PR #11.

## Context

A `claude --print` worker is spawned by the dispatcher with
`stdout=PIPE`/`stderr=PIPE`, and the dispatch loop reads those pipes
(`runner.dispatcher._dispatch_loop`). Each worker runs inside a
`DispatchSlot.thread` owned by the supervisor process.

This couples a worker's survival to the supervisor's:

1. **The pipe dies with the supervisor.** A pipe's read end lives in
   the supervisor. When the supervisor exits, the next write the worker
   makes to stdout raises `SIGPIPE`/`EPIPE` — so workers do not reliably
   survive a supervisor exit at all, and even if they did, a fresh
   supervisor cannot recover the output stream of a process it did not
   fork.
2. **In-flight work is lost on restart.** On startup `in_flight_slots`
   is empty; the startup reaper (`reconcile_silent_orphans`) plus the
   broad `reconcile_orphans` sweep demote `status="running"` tasks so
   they re-dispatch from scratch. A `systemctl restart` therefore either
   waits out a long graceful drain (today's `ExecStop=supervisor drain
   --no-wait` + `TimeoutStopSec=4h`, joining live dispatcher threads) or
   abandons partially-complete runs.

Operators upgrading the runner (e.g. `git pull` + `systemctl restart`)
must currently choose between a slow drain and losing work. With long
literature-extraction tasks in flight, neither is acceptable.

## Decision

Make workers survive a supervisor restart and let a fresh supervisor
**adopt** them. Three coordinated changes, gated by
`[supervisor].adopt_workers` (default **true**):

1. **File-backed worker output.** Spawn `claude --print` with `stdout`
   and `stderr` redirected to per-attempt files under
   `<queue>/.claude_task_runner/logs/<task-id>/attempt-<n>.stream.jsonl`
   (and `.stderr`), keeping `start_new_session=True`. The worker writes
   to its own files and is unaffected by the supervisor's lifecycle (no
   `EPIPE`). `TaskState` records `log_path` so any supervisor incarnation
   can find the stream.

2. **One worker interface, two backings.** The dispatch loop consumes a
   line *tailer* over the stdout file instead of `process.stdout`. A
   small `_Worker` abstraction exposes `alive()`, `lines()` (tail until
   exit), `terminate()` (process-group SIGTERM→SIGKILL via `killpg`),
   and the post-run exit signal:
   - `OwnedWorker` wraps the live `Popen` (`alive()=poll() is None`,
     exact `returncode`). This is the normal same-incarnation path.
   - `AdoptedWorker` wraps `(pid, log_path)` with no `Popen`
     (`alive()=os.kill(pid,0)` succeeds; completion = pid-gone; outcome
     inferred from the terminal stream-json `result` event in the log,
     since we cannot `wait()` a process we did not spawn).

3. **Startup adoption + fast stop.**
   - On startup, before the demotion sweep, for each `status="running"`
     task whose recorded `pid` is alive and whose verdict is HEALTHY,
     reconstruct a `DispatchSlot` whose thread re-tails the log to
     completion and finalizes it (writing the RunRecord, honouring the
     ADR-0020 output gate). Adopted tasks are shielded from
     `reconcile_orphans`. SILENT/KILL/dead-pid tasks keep today's reaper
     behaviour.
   - On stop, when adoption is enabled the supervisor stops dispatching
     and exits **immediately** without joining worker threads — workers
     keep running file-backed and are adopted by the next supervisor.
     `systemctl` `ExecStop` points at this fast stop, and
     `TimeoutStopSec` drops from 4h to a short bound.

     > **Amended (2026-09-26):** until this date the stop was not
     > immediate. The signal handlers only set flags, and the loop read
     > them only at the top of a tick, after
     > `time.sleep([usage].poll_interval_s)` (60 s by default). Python
     > resumes a sleep after a handler that does not raise (PEP 475), so
     > a stop waited for the rest of the tick and the whole sleep. On the
     > live runner `supervisor stop` took 46 s: the supervisor logged the
     > SIGTERM at 18:05:55.87 UTC and exited at 18:06:41.98 UTC. Under
     > `systemctl stop` or `restart`, systemd sends SIGKILL once
     > `TimeoutStopSec` (30 s) has passed, and SIGKILL skips the `finally`
     > that removes `supervisor.pid`. Now the sleep runs in slices of
     > `supervisor.daemon.SIGNAL_CHECK_INTERVAL_S` (0.5 s), and ends at
     > the first check after a stop or drain signal. A stop that arrives
     > during a tick ends the tick once its usage poll returns, before
     > anything is dispatched: a new dispatch would start work that the
     > exit then abandons. The poll still runs to the end. The part of a
     > tick before its dispatch phase takes about 8 s on the live runner,
     > but a TTY capture that runs into its `[usage]` capture timeouts
     > can take longer than `TimeoutStopSec`. SIGHUP still waits for the
     > next scheduled tick.
     >
     > The slow sleep had hidden a race in "exits without joining worker
     > threads". A dispatch thread starts its worker with `Popen` and only
     > then records the pid and log path in the task's state. An exit
     > between the two leaves a worker that the next supervisor can
     > neither adopt nor see, so it demotes the task and dispatches it
     > again, and two workers run it. Now each dispatch thread holds a
     > `runner.spawn_gate.SpawnGate` from just before it opens the log
     > files until the pid is on record. Before it releases the supervisor
     > lock, a stopping supervisor closes the gate and waits for the
     > threads inside, for at most `supervisor.daemon.WORKER_START_WAIT_S`
     > (10 s), and logs an error naming any task still inside. A daemon
     > thread that reaches the gate after that starts nothing; its task
     > stays `running` with no pid, and the next supervisor demotes it and
     > dispatches it once. A thread still in its pre-dispatch hook at exit
     > is not waited for, and the hook's process outlives it.

Net: `systemctl restart` becomes near-instant *and* loses no in-flight
work.

## Consequences

- **New on-disk artifact.** Per-attempt stream logs accumulate under the
  queue's `logs/` dir. They double as the per-attempt transcript the
  architecture doc previously claimed but never produced. (Retention is
  a follow-up; not addressed here.)
- **Adopted exit codes are inferred,** not exact: an adopted worker's
  success/failure comes from its terminal `result` event (+ stderr
  tail), not a numeric `returncode`. Owned workers are unchanged.
- **The reaper and an adopt-monitor can race** on the same task between
  the monitor's finalize and the reaper's demotion. The existing
  `_demote_if_still_running` recheck guard (re-reads `status` before
  writing) already covers this; adopted slots additionally appear in
  `in_flight_slots` so the per-tick reaper sees fresh heartbeats.
- **Kill-switch:** `[supervisor].adopt_workers = false` restores the
  pipe-backed, drain-on-stop, demote-on-restart behaviour for a
  conservative rollout.
- **`KillMode=process` stays required** in the unit so systemd never
  signals the worker group on supervisor stop.

## Amendment (2026-09-26) — workers that exit in the restart gap

**What happened.** During a planned restart of the live supervisor, the old
supervisor exited after `supervisor stop` while a file-backed worker kept
running (`KillMode=process`). The worker finished 53 s later; its stream log
ends in a `result` event, subtype success, and its work was committed and
pushed. The new supervisor started 6 s after that. Its startup sweep found the
task `running` with a dead pid and demoted it: status `failed`, stop_reason
`orphaned_by_supervisor_restart`, no RunRecord, no session id. A `failed` task
is re-dispatched, so finished work would have run again. It was finalized by
hand from the log.

**Why.** Decision 3 grouped a dead pid with SILENT and KILL survivors: "keep
today's reaper behaviour". That behaviour was designed for pipe-backed
workers, whose output dies with the supervisor that owned the pipe. A
file-backed worker's log outlives both of them and says how the worker ended.
The startup passes never read it.

**Decision.** At startup, after the corrupt-state quarantine (ADR-0028) and
before every other recovery pass, `supervisor.adoption.finalize_exited_workers`
looks at each `running` task with a recorded pid that is dead and a log file
that exists. When the log ends in a terminal `result` event, the attempt is
finalized as the adoption monitor finalizes an adopted worker whose pid is
gone (`runner.dispatcher.finalize_exited_worker`, through `_finalize_adopted`).
It gets one RunRecord built from the result, the same completed/failed
classification (the ADR-0020 output gate, an open sidecar's
`awaiting_sidecar`), and the same recheck guard, so a concurrent writer's
record is never clobbered. A log without a result event is left alone. That
worker crashed or was killed, and the silent-orphan reaper and
`reconcile_orphans` handle it as before.

- **Ordering.** The pass must precede the silent-orphan reaper. A finished
  worker's heartbeat is as stale as a hung one's: after a gap longer than
  `[task_caps].heartbeat_silence_alert_s`, the reaper would park the task
  `possibly_hung`; after a shorter one, `reconcile_orphans` demotes it.
- **Times.** `finished_at` is the log's last write (its mtime, capped at now
  and floored at the attempt's start), not the restart that found it, so
  `duration_s` and `last_finished_at` exclude the supervisor's downtime.
- **No caps.** Per-task caps stop a live worker. This one ended on its own,
  and its result is the record of how.
- **Account.** A first attempt's state records no account until it finalizes.
  The run is recorded under the account in the previous supervisor's persisted
  `in_flight` record for the task, when the queue still declares that
  account, and otherwise under the state's session host account (ADR-0024).
- **Kill switch.** `[supervisor].adopt_workers = false` turns this pass off
  with the rest of this ADR.
- **Per-tick passes are unchanged.** The per-tick silent reaper considers only
  tasks with a live owner thread in this supervisor, a dispatch or adoption
  monitor, and that thread finalizes from the log when the worker exits.
  `reconcile_orphans` runs only at startup.

**Consequences.** A worker that finishes in the restart gap is recorded as its
log says, and a completed task is not re-dispatched. The finalize is the
adopted path's, and it now runs the owned path's post-run steps too:

- **The ADR-0020 output gate counts a commit.** The attempt's pre-dispatch
  `HEAD` is recorded on the running state as `pre_dispatch_sha`, written
  before the worker spawns and cleared at finalize. Before this, the adopted
  finalize had no SHA to compare, so a worktree task whose only output was a
  pushed commit failed `end_turn_no_output` and was re-dispatched.
- **The ADR-0033 terminal-close gate and the ADR-0027 sidecar re-file guard
  run.** Both need to know whether the run committed. A state written before
  `pre_dispatch_sha` existed can't show that, so for such a state the two stay
  off, as before, and a commit alone still does not count as output. Without
  that exception, the terminal-close gate would take a committed, reported
  run for a skip and write a block row.
- **The post-dispatch hook runs**, whether or not the recheck guard stands
  down, since the worker has exited either way. It runs best-effort: a hook
  that fails, or cannot start, logs a warning and never fails the finalize.
  On the owned path, a hook that could not start used to raise out of the
  dispatch after the run was recorded. For a worker that exited in the
  restart gap, the hook runs during startup, before the first tick.
