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

## Update (2026-10-15)

An adopted worker's slot now names the account it runs on from the same
sources as the exited-worker finalize: the previous supervisor's persisted
`in_flight` record for the task when the queue declares that account, then
the state's session host account, then the only account of a
single-account queue. It used the session host alone, so a first attempt
(no session yet) was counted against an account named `default`, which a
multi-account queue does not have: after a restart its real account could
be dispatched past its own `max_concurrency` until the worker finished.
When no source names an account the slot names `(unknown)`, which counts
against no account's cap, and a WARNING says so (ADR-0035).

## Amendment (2026-10-16) — adopt every worker that shows life; never demote a live one

**What happened.** On 2026-10-16 at 13:24 UTC the live supervisor was
restarted for a deploy (`systemctl --user restart`, `KillMode=process`).
The new supervisor adopted one of three running workers. The other two,
task `oasweep_PMC8247401` (pid 512811) and task `oasweep_PMC8329445` (pid
395266), were in long Bash calls. Their stream logs were still being
written (last writes 13:24:35 and 13:24:39), but their `last_heartbeat_at`
read 13:15:25 and 13:17:52. The startup reaper found both HEALTHY, because
the old supervisor's monitor had written `dispatcher_alive_at` seconds
before it stopped. Adoption graded `last_heartbeat_at` alone, found it
past `heartbeat_silence_alert_s`, and did not adopt them. `reconcile_orphans`
then set both tasks `failed` (`orphaned_by_supervisor_restart`). It also
clears the persisted `in_flight` list, so nothing held their accounts'
slots, and the first tick dispatched both tasks again at 13:24:42 into the
same worktrees, beside the two workers still running there.

**Why.** Three causes.

1. The agent heartbeat does not advance during a tool call. The dispatch
   loop updates `last_heartbeat_at` only on the events `parse_lines`
   yields (`system/init`, `assistant`, `user`, `result`), and writes it at
   most once per `heartbeat_persist_interval_s` (30 s). `tool_progress` is
   not yielded, on purpose: it arrives while a tool runs, and counting it
   would make a worker stuck in a Bash poll loop look alive to the
   stuck-sleep-loop reaper. In the two logs the Bash calls ran 590 s and
   570 s, and claude wrote a `heartbeat: true` `tool_progress` event every
   30 s through each of them (elapsed 30, 60, ... 570), so the logs' mtimes
   advanced every 30 s while `last_heartbeat_at` stood still. A stale
   heartbeat during a long tool call is therefore expected, and the log is
   a sign of life on a steady beat.
2. Adoption and the reaper graded the same task differently. The reaper
   counted `dispatcher_alive_at`; adoption counted only `last_heartbeat_at`.
3. `reconcile_orphans` demoted every running task it was not told had been
   adopted, without asking whether its worker still ran. `_pid_alive` could
   not have said whether a live pid was still the worker anyway: it checks
   only that some process has the pid.

**Decision.**

1. **One classifier.** `supervisor.reconcile_silent.liveness_verdict`
   returns the verdict both reaper passes act on, without acting, and
   adoption adopts only a HEALTHY worker by it. Its signs of life are
   `dispatcher_alive_at` and the last write to the attempt's stream log,
   whichever is later; either within `heartbeat_silence_alert_s` means
   HEALTHY, as does an agent heartbeat within it. The log gets every event
   the worker writes, `tool_progress` included, and it is the only sign of
   life a worker keeps up while no supervisor runs. It counts as evidence
   that the worker runs, not as the agent's heartbeat: the stuck-sleep-loop
   check still keys on `last_heartbeat_at`, so a stuck poll loop writing
   `tool_progress` is still killed. The two passes differ in what the
   verdict leaves out: the startup reaper also keeps running a worker whose
   worktree had a file written within `zombie_verify_fs_activity_window_s`
   (Layer 3, recorded as `last_heartbeat_at`) and one it cannot grade (no
   `last_started_at`, a timestamp in the future). Adoption takes such a
   worker only if that recorded heartbeat is within the alert window;
   otherwise `reconcile_orphans` parks it (3).
2. **Worker identity.** `runner.dispatcher.probe_worker` returns ALIVE,
   DEAD or REUSED (another process now has the pid), and
   `probe_recorded_worker(state, queue_dir)` hands it a state's whole
   record; every pass that acts on a recorded worker asks it. A zombie is
   DEAD. When `/proc/<pid>/fd/1` and the log can both be read, they are the
   same file (device and inode) for the worker and for nothing else.
   Otherwise (a worker under another Linux user, or pipe-backed) the probe
   compares the boot id and start tick that `dispatch()` records at the
   spawn (from `/proc/sys/kernel/random/boot_id` and field 22 of
   `/proc/<pid>/stat`); neither moves with a clock step. They are written,
   before the pid, to a per-attempt file beside the stream log,
   `logs/<task>/attempt-<N>.worker`, not to the state YAML, and used only
   when that file names the state's pid. A worker without one falls back
   to the process starting in the current boot between 60 s before and
   300 s after `last_started_at`. Without `/proc` only existence is
   known. The callers: the exited-worker finalize and adoption at startup,
   the adoption monitor's tail and terminate, both reaper passes before any
   process-tree scan or signal (their terminate, too, so a zombie counts as
   gone), `reconcile_orphans`, the parked-worker pass, the dispatch guards
   and the orchestrator's leak check (the last by start time alone, as a
   RunRecord carries no log or spawn identity).
3. **A live worker is never set to a dispatchable status.**
   `reconcile_orphans` parks a running task whose worker still runs as
   `possibly_hung` with stop reason `worker_still_running`
   (`runner.dispatcher.park_live_worker`), keeping `pid`, `log_path` and
   `attempts`, naming the pid in `error`, logging a WARNING and notifying
   the operator (`live_worker_parked` event). "Still runs" means the
   recorded worker, or, when no state records one (its pid was never
   written, `UNTRACKED-PID`), a process whose stdout is one of the task's
   stream logs (`runner.dispatcher.live_worker_record`), recorded with the
   attempt number from that log's name and its own process start time.
   Every pass that demotes a task because its worker is gone clears both
   `pid` and `log_path`. A reaper KILL
   verdict terminates the worker's process group (SIGTERM, then SIGKILL,
   then a check), as the stuck-loop kill already did, instead of sending
   one SIGTERM to the pid; the task goes to `failed` only once the worker
   is gone, and a worker that survives is parked the same way. Neither
   reaper pass signals or scans a pid that the probe does not report
   ALIVE. A SILENT verdict on a worker that still runs parks it the same
   way. The SILENT park of a task whose worker is gone (`silent_on_restart`,
   `silent_steady_state`) is unchanged except that its `error` names the
   pid and the age of each sign of life. Every reap logs at WARNING.
4. **A parked worker is accounted for.** Each tick,
   `supervisor.adoption.ParkedWorkerTracker` counts every parked worker
   that still runs against the account it runs on, from the sources
   adoption uses (the previous tick's or the previous supervisor's
   `in_flight` record, which `reconcile_orphans` keeps for a parked task;
   the session host account; the only account), so `dispatch_headroom`,
   `choose_account`, `supervisor.json` and `account list` see it. One no
   record attributes counts against an explicit `[concurrency].max_concurrency`
   only. A task the selector parks counts from the same tick. A parked
   worker is held to its task's duration cap as a monitored one is: past
   it, the pass terminates its process group through the reapers'
   terminate and records the run as killed by the cap. Once the worker
   exits or is stopped, its run is recorded from its log (a crash, without
   a `result` event, or with no log) on a thread that holds the task's
   slot, as a dispatch thread holds it through its post-dispatch hook, and
   the record goes. A run that cannot be recorded is logged once per
   distinct error and retried after a delay that doubles from 60 s to an
   hour. The startup exited-worker finalize records a parked worker that
   exited while no supervisor ran.
5. **Consumers refuse a live worker's task.** Whatever produced the state,
   the selector skips (`live_worker`) and parks a dispatchable task whose
   recorded worker still runs, and the pre-spawn backstop in
   `_dispatch_one_safely`, which every dispatch path goes through, does the
   same for a worker found by `live_worker_record`; both report the park as
   the startup does, once. `queue force-dispatch` refuses a task parked
   with its worker whatever its flags, naming the worker and how to
   release it; `--allow-live-worker` lets it dispatch a pending, failed or
   deferred task (or one with no state yet) whose worker no state records. The corrupt-state quarantine
   (ADR-0028), which would revert a running task to pending, reads the
   worker's fields from the readable head of the file and parks the task
   instead when its worker still runs.
6. **The spawn writes a fresh record.** `dispatch()` clears `pid` and
   `log_path` in its running write before the spawn and always writes both
   after it, `log_path` `None` for a pipe-backed worker, so a new attempt
   never carries the worker of the one before it.
7. **Thinking counts.** `system/thinking_tokens`, which claude writes while
   a model produces extended thinking, now advances the agent heartbeat
   (`runner.stream.ThinkingEvent`). It never arrives while a tool runs, so
   it does not hide a stuck poll loop.

**Consequences.**

- A restart adopts every worker that showed a sign of life within
  `heartbeat_silence_alert_s` (300 s by default) before it, so a deploy
  restart changes nothing for a worker the old supervisor was monitoring,
  and a worker in a long Bash call, whose log is written every 30 s, is
  adopted after a downtime of any length.
- A worker that is alive but showed no sign of life for longer than that
  is parked `possibly_hung`, not dispatched again. It holds its account's
  slot until it exits or reaches its duration cap, and its run is then
  recorded within a tick. An operator who wants it released sooner stops
  its process group (runbook: "After a restart, a task is failed or
  possibly_hung while its worker still runs").
- With `[supervisor].adopt_workers = false` nothing is adopted, and a
  worker still alive at startup is parked instead of demoted. A dead one
  is demoted as before.
- The reaper functions take `terminate_fn` for both kill paths and a
  `worker_probe_fn`; `sigterm_fn` is gone.
- `TaskState` has no new fields, so rolling back to an earlier runner
  leaves every state readable. The spawn identity lives in the
  `attempt-<N>.worker` files, which an earlier runner ignores. A state
  that carries `worker_boot_id` or `worker_start_ticks` inline (written by
  a build of this change that kept them there, never deployed) loads with
  the two keys dropped.
- On the live queue `heartbeat_silence_kill_s` is 0 (the default), so the
  KILL verdict never fires there; the per-tick reaper kills only a
  detected stuck sleep loop, and then only after
  `stuck_sleep_loop_kill_threshold_s` (600 s) of agent silence with a
  matching loop in the worker's process tree.

## Amendment (2026-10-30) — the log so far is the adopted worker's past

**What happened.** Adoption tailed the stream log from its first byte,
through the same dispatch loop as a new worker's lines. Every line already
in the log was written before the restart, but the loop stamped the agent
heartbeat with the time it read each one, so the first event persisted
`last_heartbeat_at` at the adoption time. A worker stuck in a poll loop
since before the restart then read as freshly heard from. The
stuck-sleep-loop check keys on `last_heartbeat_at` (decision 1 of the
2026-10-16 amendment), so it waited a full
`stuck_sleep_loop_kill_threshold_s` from the restart before it looked. The
silence `heartbeat_silence_kill_s` measures (ADR-0037) also ran from the
restart.

**Decision.** The complete lines the log holds when the monitor attaches
(`runner.dispatcher._log_backlog`) go through the dispatch loop as its
backlog. They fill the run's summary, record the session and meet the
per-event caps, but they stamp no heartbeat, and the tailer starts after
them. `last_heartbeat_at` keeps the value the last supervisor wrote until
the worker writes an event after the restart. The silence
`heartbeat_silence_kill_s` measures runs from the log's last write, held
between the attempt's start and now.

**Consequences.**

- After a restart, the stuck-sleep-loop check and `heartbeat_silence_kill_s`
  see the agent's silence since before it, not since the adoption.
- An agent event the last supervisor did not persist leaves
  `last_heartbeat_at` older than the agent's last event: one written
  within `heartbeat_persist_interval_s` of its last write, or while no
  supervisor ran. Dating the backlog's events by the log's last write
  instead would bring back the reset, since during a tool call that write
  is a `tool_progress` line's, not the agent's. The older heartbeat only
  makes the stuck-sleep-loop check look sooner, and that check acts only
  on a sleep loop under the worker.
