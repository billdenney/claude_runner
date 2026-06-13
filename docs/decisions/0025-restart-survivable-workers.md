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
