# Operator runbook

Quick reference for common situations. Each scenario links to the
relevant component / ADR for deeper context.

## Parser drift detected

**Symptom:** `claude-task-runner supervisor status` shows state
`error_drift` and a `Last drift:` line with the parser's message. The
supervisor log has a `notify[error]: parser drift: ...` line from the tick
that entered `ErrorDrift` (journald under the systemd unit,
`<queue>/.claude_task_runner/supervisor.log` under the cron watchdog; see
[where the log goes](architecture.md#supervisor-log-and-drift-evidence)).
`claude-task-runner usage healthcheck`, which always captures through the
TUI, exits 1. No desktop notification is sent, and there is no separate
drift log.

**Causes:** Anthropic changed the `/usage` TUI layout. Most common forms:
field rename (`Resets` → `Resets at`), new section added, ANSI escape
sequence variant.

**Steps:**
1. Get the raw output. With the TTY usage source, the capture that failed
   to parse is already on disk as the newest
   `<queue>/.claude_task_runner/usage_captures/<ts>.cap`. To record a fresh
   one, run `claude-task-runner usage capture --save /tmp/drift-$(date +%s).cap`.
2. `claude-task-runner usage parse-file <path-to.cap>` to see the parse
   failure.
3. Inspect the `.cap` (cat with ANSI rendering) to identify what changed.
4. Update `usage/parser.py` state machine (or add a fixture variant).
5. Run `pytest tests/unit/test_parser.py` until green.
6. Commit fixture under `tests/fixtures/usage/<date>_<reason>.cap` so the
   regression is locked in.
7. Save the fix-validating fixture as `*.keep` to exempt from rotation.
8. Restart supervisor. After 3 consecutive clean polls, it leaves
   `ErrorDrift`.

## An account stays in `no_reading`

**Symptom:** `claude-task-runner account list` shows an account in state
`no_reading` with `in_flight=N/0`, and dispatch sends it no tasks. When a
reading went stale, the supervisor log has one
`notify[warn]: no clean usage reading for account '<name>' since <time>,
over the <limit> s limit; ...` line.

**Expected at start:** every account starts in `no_reading`, including
after the upgrade to `supervisor.json` schema 7, and leaves it at its first
clean reading, within one capture cycle (one poll per account). No notice
is sent for that.

**Causes when it persists:** that account's `/usage` capture keeps timing
out or failing to spawn. Those failures log only DEBUG `usage_capture_error`
events, and they don't count as readings, so after
`[usage].max_reading_age_s` (600 s by default) the account stops taking
tasks. Other causes: the supervisor was down for longer than the limit, or
`[[accounts]]` changed and only a SIGHUP followed. A reload doesn't rebuild
the usage source, so an account added that way is never read.

**Steps:**
1. Set `[logging].level = "DEBUG"` and look for `usage_capture_error`
   events.
2. Capture as that account by hand: `CLAUDE_CONFIG_DIR=<its config_dir>
   claude /usage`. On a single-account queue,
   `claude-task-runner usage healthcheck` does the same.
3. Fix what the capture shows (login, network, the config directory). The
   account leaves `no_reading` at its next clean capture. Meanwhile
   `claude-task-runner queue force-dispatch <task_id>` bypasses this check,
   as it bypasses the throttle.
4. After changing `[[accounts]]`, restart the supervisor.

## Fewer tasks in flight than the accounts' caps allow

**Symptom:** The accounts are dispatching, not paused, and tasks are
eligible, but the queue's total in flight stops at one number below the sum
of the accounts' caps: `claude-task-runner account list` shows an account
with free slots (`in_flight=2/5`) that receives nothing.

**Cause:** `[concurrency].max_concurrency` in `claude_runner.toml` is set
below the sum of the accounts' own `max_concurrency` (their
`runner-account.toml` files), and caps the whole queue. Without that key,
the per-account caps are the only limit (ADR-0035). The supervisor log has
a line like this once per config load, and again whenever either number
changes:

```
[concurrency].max_concurrency = 5 holds the queue below its accounts' own caps, which sum to 8 (personal 5 + work 3): at most 5 tasks run at once. ...
```

Before a first task completes in the queue, a set
`[concurrency].initial_concurrency` caps it the same way.

**Steps:**
1. Find the ceiling the supervisor uses: it logs
   `concurrency ceiling: ...` at INFO at start and after each SIGHUP
   reload, e.g. `concurrency ceiling: 5 from
   [concurrency].max_concurrency, which binds: the accounts'
   max_concurrency sum to 8 (personal 5 + work 3)`. `claude-task-runner
   supervisor status` (or `account list`) prints the same line for the
   files as they are on disk now.
2. To let each account run up to its own cap, delete `max_concurrency` (and
   `initial_concurrency`, if set) from `[concurrency]` in
   `claude_runner.toml`, then send the supervisor SIGHUP (`kill -HUP` the
   PID in `<queue>/.claude_task_runner/supervisor.pid`); it applies the
   change on its next tick and logs `concurrency ceiling: 8, the sum
   of the accounts' max_concurrency (...)`.
3. To change one account's share, edit that account's
   `<config_dir>/runner-account.toml`. The supervisor re-reads it every
   tick; no SIGHUP is needed.
4. Keep `max_concurrency` only as a deliberate global cap. The warning
   repeats on each reload while it binds.

## An account file is broken

**Symptom:** One account gets no new tasks while the others dispatch as
usual. The supervisor log has, once per error (and again after each edit
that leaves the file broken):

```
account 'work' takes no new tasks until its runner-account.toml loads: Invalid TOML in /home/bill-work/.claude/runner-account.toml: Expected '=' after a key in a key/value pair (at line 3, column 17)
```

`claude-task-runner account list` prints `policy error:` with the same text
for that account and exits 1; `supervisor status` prints it as
`Account error:`; `doctor` fails `account_policies`. When no account's file
loads, the log also says `no account's runner-account.toml loads (...):
nothing is dispatched until one is fixed`.

**Cause:** The account's `<config_dir>/runner-account.toml` is there but
cannot be read, is empty (zero bytes, often a save that was cut short), is
a symlink to a file that does not exist, is not TOML, still has a
`[throttle.*]` table, or sets a value the schema rejects (a
`max_concurrency` of 0, an unknown key). A `config_dir` starting with
`~user` for a user that does not exist is reported the same way. The
supervisor does not guess a policy for it (ADR-0035): the account takes no
new tasks, its running tasks finish as usual, and it adds nothing to the
queue's concurrency ceiling. Only an absent file means the defaults.

**Steps:**
1. Fix the file the error names. `claude-task-runner account list` exits
   0 again once every account's file loads.
2. Nothing else is needed: the supervisor re-reads the file every tick,
   logs that the account `takes tasks again: <path> loads`, and dispatches
   to it on that tick. If the file was broken for longer than
   `[usage].max_reading_age_s`, the account waits for its next clean usage
   reading first, within one capture cycle.
3. Deleting the file instead gives the account the defaults
   (`max_concurrency = 1`, the queue's `[dispatch_pct.*]`); the log line
   then says the file `is gone, so the defaults apply`. An empty file is
   not the same as no file: it stays broken until it is deleted or filled
   in.

## Supervisor crashed repeatedly (watchdog crash-loop)

**Symptom:** `~/.claude_task_runner/watchdog.log` shows multiple restarts
in a short window. `<queue>/.claude_task_runner/supervisor.log` ends with the
same exception each time.

This section covers the cron watchdog. Each tick takes the managed queue's
`[watchdog]` settings from the config that `install --config` or
`claude-task-runner watchdog register --config` recorded for it, else from
`<queue>/claude_runner.toml`, else the package defaults, and starts the
supervisor with the same file. A change applies from the next tick.

Under the systemd unit, systemd
restarts the supervisor itself (`Restart=on-failure`), `RestartSec` after
each crash, and stops once it has started the unit more than
`StartLimitBurst` times within `StartLimitIntervalSec`. `install` writes
those three from the queue's `[watchdog].restart_cooldown_s`,
`[watchdog].crash_loop_threshold` and `[watchdog].restart_backoff_max_s`
(30 s, 5 and 600 s by default). After changing them, re-run
`claude-task-runner install`: it rewrites the unit and reloads systemd, and
the next crash uses the new values without the supervisor being restarted.
Read the exception with `journalctl --user -u claude-task-runner`. Once the
cause is fixed, `systemctl --user reset-failed claude-task-runner` clears
the start limit, and `systemctl --user start claude-task-runner` starts the
supervisor again.

**Steps:**
1. Check that the watchdog is backing off. It counts the restarts since
   the supervisor last stayed up `min(10 × restart_cooldown_s,
   restart_backoff_max_s)`, 300 s by default. After
   `[watchdog].crash_loop_threshold` of them (5 by default), ticks log
   `verdict=backoff` with `backing off until <time>`. The wait from one
   restart to the next doubles, from twice the cooldown up to
   `[watchdog].restart_backoff_max_s` (600 s by default). So a supervisor
   that dies on every start is restarted about six times an hour, not every
   minute. Once it stays up 300 s, the count starts over. Nothing sends a
   notification; `watchdog.log` is the record.
2. Read `supervisor.log` for the failing exception. Common causes:
   - Disk full → `usage_captures/` rotation hadn't run; clear old captures.
   - Settings TOML invalid → `claude-task-runner doctor` (loads the TOML
     through the schema and reports the offending field).
   - A leftover `~/.claude_task_runner/global.lock` is not a cause. The OS
     releases the lock when its holder dies, and the next supervisor locks
     the same file. `claude-task-runner doctor` reports under `global_lock`
     whether a live supervisor holds it. Do not remove the file while one
     does: the next supervisor would lock a new file and run beside it.
3. Once root cause is fixed, manual restart: `claude-task-runner supervisor start`.

## Task `possibly_hung` for hours

**Symptom:** A task's state shows `possibly_hung` and last_heartbeat is
N hours old.

**Steps:**
1. Read `<queue>/.claude_task_runner/logs/<id>/attempt-N.stream.jsonl` —
   does it show partial progress, or is it truly silent?
2. Check the underlying `claude` PID via `ps -ef | grep <session_id>`.
   If the process is alive but not emitting events, it may be hung on
   an API call. Kill it: `kill <pid>`.
3. The runner will mark the task `failed` (environmental); on next tick
   the supervisor will dispatch via `--resume <session_id>` automatically.
4. If hangs are common, reduce `task_caps.heartbeat_silence_kill_s` from
   default 0 (off) to e.g. 600 to auto-kill silent tasks.

## After a restart, a task is `failed` or `possibly_hung` while its worker still runs

**Symptom:** after `systemctl --user restart claude-task-runner` (or any
supervisor restart), `claude-task-runner queue states` shows a task as
`possibly_hung` with stop reason `worker_still_running` or
`silent_on_restart`, but its `claude` worker is still running and its
stream log is still growing. A runner older than 2026-10-16 showed such a
task as `failed` with stop reason `orphaned_by_supervisor_restart`, and
dispatched it again beside the running worker.

**Cause:** workers survive a restart (`KillMode=process`) and the new
supervisor adopts each one that showed a sign of life within
`[task_caps].heartbeat_silence_alert_s` (300 s by default): an agent event,
a monitor tick from the previous supervisor, or a write to its stream log.
A worker that showed none for longer than that, for instance in a long
Bash call after a long downtime, or every live worker when
`[supervisor].adopt_workers` is off, is parked instead: a `failed` task
would be dispatched again, and two workers in one worktree corrupt its
branch. Nothing monitors a parked worker, so its task stays `possibly_hung`
after the worker exits.

**Steps:**
1. Find the worker. The state's `error` names its pid
   (`claude-task-runner queue show <id>`). Check that the pid is still the
   worker: `readlink /proc/<pid>/fd/1` prints the attempt's log,
   `<queue>/.claude_task_runner/logs/<id>/attempt-N.stream.jsonl`.
2. Check for a second worker on the same task. Every worker's working
   directory is the task's worktree:
   `for p in $(pgrep -f 'claude --print'); do echo "$p $(readlink /proc/$p/cwd)"; done`.
   If two share the worktree, first add a `"block_dispatch": true` row for
   the task to the `[dispatch].dispatch_block_file` (ADR-0029), so the
   supervisor does not dispatch it a third time, then stop the newer
   worker (its log has the higher attempt number) with `kill -TERM -<pid>`
   (the minus signals its process group). Remove the row once the task is
   recorded (step 4).
3. Let the worker finish: `tail -f` its log until it writes a `result`
   event, and wait for the pid to exit.
4. Record the run. Stop the supervisor, set `status: running` in
   `<queue>/.claude_task_runner/state/<id>.yaml` (a `worker_still_running`
   park keeps `pid` and `log_path`, which is what the next step needs), and
   start the supervisor. Its startup pass records the run from the log's
   `result` event, as for any worker that exited during a restart. A
   `silent_on_restart` park has no `pid` left; set it to `failed` instead,
   and the supervisor resumes the session.

## Weekly throttled, no dispatch resuming

**Symptom:** Supervisor in `ThrottledWeekly`, observed weekly
utilization above the trace target for the current elapsed fraction.

**Steps (ADR-0022 — variant-C trace-following):**
1. Compute the current curve target: at elapsed fraction `t`, the
   target is `(t / (1 - eow_frac)) * early_pct` (pre-EOW) or
   `early_pct + ((t - (1 - eow_frac)) / eow_frac) * (eow_pct - early_pct)`
   (EOW segment). With defaults `early_pct=60`, `eow_pct=95`,
   `eow_time_switch="40h"`, the EOW elbow is at `t ≈ 0.76`.
2. The supervisor's `scheduled_wakeup_at` should already point to
   the analytical catch-up time (when the curve rises to meet
   observed); confirm via `claude-task-runner supervisor status`.
3. If catch-up is far in the future and you have urgent work, either
   raise `[dispatch_pct.week].early_pct` for the queue, or use
   `claude-task-runner queue force-dispatch <task_id>` to bypass
   the throttle for that one task.

## Sidecars piling up

**Symptom:** `claude-task-runner queue states --status awaiting_sidecar`
shows many tasks waiting.

**Steps:**
1. From Claude Code, `/runner-answer-sidecar` lists all open sidecars.
2. Walk through them; each click resolves one (no typing needed for the
   common case).

## A task stays `deferred`

**Symptom:** `claude-task-runner queue states --status deferred` lists a
task that does not run.

**Steps:**
1. Read the `deferred_reason` line printed under the task.
   `claude-task-runner queue show <task_id>` prints it for one task.
2. The start of the reason says what parked the task:
   - `readiness hold:` — a `requires` element is missing (ADR-0030). The
     task goes back to `pending` on the first tick after it appears.
     `queue show` also lists the unmet elements, checked live.
   - `invalid effort:` — the queue's `[effort_levels]` does not accept the
     task's (model, effort) pair (ADR-0010). Fix the task's model or effort,
     or add the pair to `[effort_levels]` and send the supervisor SIGHUP.
     The task goes back to `pending` on the next tick.
   - `pre-dispatch hook deferred (exit 1):` — the pre-dispatch hook asked
     to wait, and the rest of the reason is its stderr (ADR-0026). Once the
     `next_eligible_at` printed below the reason has passed, the supervisor
     tries the task again and runs the hook again.
   - Anything else was written by hand. The supervisor treats the task as
     ready again once its `next_eligible_at` has passed, or at once when it
     has none.

## Nothing dispatches, but an account has room

**Symptom:** No task has been dispatched for hours or days, nothing is in
flight, and `claude-task-runner supervisor status` shows an account that is
not paused or throttled.

**Steps:**
1. Confirm the supervisor is still ticking: `claude-task-runner supervisor
   status` reports it alive, and `<queue>/.claude_task_runner/supervisor.json`,
   which it rewrites every tick, was modified within the last poll interval
   or two. The log line below cannot show that on its own, since it is
   logged only when something changes.
2. Find the last line about eligible tasks in the supervisor log
   ([where the log goes](architecture.md#supervisor-log-and-drift-evidence)),
   e.g. `journalctl --user -u claude-task-runner | grep 'eligible tasks' | tail -1`.
   If it reads like

   ```
   no eligible tasks: 329 unfinished held (block_dispatch 178, readiness_missing_file 128, unanswered_sidecar 18, depends_on 3, deferred_cooldown 1, possibly_hung 1)
   ```

   then as of that line's timestamp the queue was drained, not stuck:
   every unfinished task in `todo/` was held. Completed tasks are not
   counted. The line is logged when the queue runs dry (and on the first
   such tick after a supervisor start), then again only when a count
   other than `in_flight` changes, so it can be days old on a quiet
   queue; step 1 is what shows the supervisor is still at work since. If
   the last line is `eligible tasks available again: N` instead, tasks
   were eligible at that tick.
3. Each count names what holds those tasks:
   - `block_dispatch` — a `"block_dispatch": true` row for the task in the
     `[dispatch].dispatch_block_file` (ADR-0029). Remove the row to release it.
   - `readiness_missing_file`, `readiness_awaiting_sidecar_response` — an
     unmet `requires` element (ADR-0030); see "A task stays `deferred`". A
     task missing elements of both kinds is counted once, under
     `readiness_awaiting_sidecar_response+missing_file`.
   - `unanswered_sidecar` — see "Sidecars piling up".
   - `depends_on` — a task named in `depends_on` has not completed.
   - `deferred_cooldown` — a `deferred` state whose `next_eligible_at` has
     not passed, whoever wrote it: a pre-dispatch hook deferral or a
     deferral written by hand. See "A task stays `deferred`".
   - `invalid_effort` — see "A task stays `deferred`".
   - A status name (`possibly_hung`, `running`, `failed_circuit_breaker`,
     `weekly_paused`, ...) — the task's state has that status, which is
     never dispatched.
   - `in_flight` — already running under this supervisor.
   - `unparseable_task`, `unparseable_state` — a task or state YAML that
     does not load; the supervisor log names the file.
4. No `no eligible tasks:` line since the last `dispatched task` line, on a
   supervisor that is ticking, means one of two things:
   - the ticks are not reaching candidate selection: every account is
     paused, throttled or without a reading, or every slot is taken;
   - there are eligible tasks, but no account will take any of them: each
     tick then logs `dispatch skipped task <id>: <reason>` per task, giving
     the account cap or session affinity that refused it.

   Look for `tick_dispatch failed` lines too.

## Cron / systemd watchdog not installed

**Symptom:** Supervisor died once (e.g., after a reboot) and didn't
come back.

**Steps:**
1. `claude-task-runner doctor` shows whether watchdog is installed.
2. `claude-task-runner install` auto-detects systemd vs cron, shows the
   proposed change, asks for confirmation. Accept it.
3. Verify. Under cron, `claude-task-runner watchdog queues` must print the
   queue, as its last line if it prints several: the watchdog manages only
   that one (see the next section if it does not). Stop the supervisor with
   `claude-task-runner supervisor stop`. The first tick after it exits
   restarts it, and `~/.claude_task_runner/watchdog.log` shows
   `verdict=restart`.

   Under systemd, `systemctl --user status claude-task-runner` shows the
   unit active. To test the restart, crash only the supervisor:

   ```sh
   systemctl --user kill --kill-whom=main --signal=KILL claude-task-runner
   ```

   On systemd older than 252, spell the option `--kill-who=main`, or run
   `kill -KILL <pid>` with the PID that
   `claude-task-runner supervisor status --queue <queue>` prints. After
   `RestartSec` (the queue's `[watchdog].restart_cooldown_s`, 30 s by
   default) the unit is active again with a new main PID, and
   `journalctl --user -u claude-task-runner` shows
   `Main process exited, code=killed, status=9/KILL`, then
   `Scheduled restart job`. In-flight `claude` workers keep running
   (`KillMode=process`). With `[supervisor].adopt_workers` on, the
   default, the new supervisor adopts them. A worker that finished
   while no supervisor ran is recorded from its log instead. The
   supervisor logs `recorded exited worker for task <id> from its log`,
   and the task gets that run's status, `completed` for a success.
   systemd also logs `Found left-over process` for each one still
   running, which is expected.

   Do not test with `kill <pid>` or `supervisor stop`. Both send SIGTERM,
   the supervisor exits 0, and `RestartPreventExitStatus=0` leaves it
   down, so `systemctl --user status` shows the unit `inactive (dead)`.
   A unit whose `ExecStop=` line has no leading `-` (see
   `systemctl --user cat claude-task-runner`) was installed before that
   prefix was added, and it shows `failed (Result: exit-code)` instead.
   Its ExecStop (`supervisor stop`, or `supervisor drain --no-wait` with
   adoption off) runs after the supervisor has gone and exits 1, and
   without the `-` systemd records that as a failure. Nothing crashed.
   Re-run `claude-task-runner install` to get the current unit. Start the
   supervisor again with `systemctl --user start claude-task-runner`. Do
   not drop `--kill-whom=main` either: the default, `all`, also SIGKILLs
   every in-flight `claude` worker in the unit's cgroup.

## A systemd `install` left the old supervisor running

**Symptom:** after `claude-task-runner install --queue <new>` under systemd,
`claude-task-runner supervisor status --queue <new>` shows no supervisor,
while `systemctl --user status claude-task-runner` shows the unit active with
a main process still running `supervisor start --queue <old>`. `install`
printed a note that the running unit keeps its supervisor, and
`systemd unit installed.` An older `install` printed
`systemd unit installed and started.` instead.

**Cause:** `install` rewrites the unit file, reloads systemd and runs
`systemctl --user enable --now claude-task-runner`. That starts a stopped
unit but does not restart a running one. systemd applies `ExecStart=`,
`WorkingDirectory=` and `Environment=` only when it starts the service, so
the running supervisor keeps its old queue and command until the unit
restarts. The restart policy (`RestartSec=`, `StartLimitBurst=`,
`StartLimitIntervalSec=`), `ExecStop=` and `TimeoutStopSec=` apply at the
reload, so a changed `[watchdog]` or `[task_caps].max_duration_s_per_task`
alone needs no restart, and `install` does not ask for one.

**Steps:**
1. To let the old queue's in-flight tasks finish first, drain its
   supervisor, then start the unit. A drained supervisor exits 0, which the
   unit does not restart.

   ```sh
   claude-task-runner supervisor drain --queue <old>
   systemctl --user start claude-task-runner
   ```

   `drain` returns once the supervisor has exited. Unless you pass
   `--timeout`, it waits as long as a task that started just before it
   may take: `[task_caps].max_duration_s_per_task`, both hook timeouts
   and one `[usage].poll_interval_s`, with no limit when the cap is 0.
   If it exits 4, the supervisor is still draining and the unit is still
   active, so `systemctl --user start` would do nothing. Run `drain` again
   before starting the unit.

2. Or switch at once:

   ```sh
   systemctl --user restart claude-task-runner
   ```

   With `[supervisor].adopt_workers` on, the default, the old queue's
   in-flight workers keep running, but no supervisor follows them until
   one runs for that queue again. That supervisor records each worker
   that finished in the meantime from its log.

## A systemd stop killed in-flight tasks (adoption off)

**Symptom:** with `[supervisor].adopt_workers = false`,
`systemctl --user stop` or `restart claude-task-runner` ends with
`State 'stop-sigterm' timed out. Killing.` and
`Main process exited, code=killed, status=9/KILL` in
`journalctl --user -u claude-task-runner`, and the tasks that were running
are dispatched again by the next supervisor.

**Cause:** with adoption off, the unit's `ExecStop` drains. The supervisor
dispatches nothing new and exits once its in-flight tasks finish. systemd
waits `TimeoutStopSec` for that, then SIGKILLs the supervisor. `install`
sets `TimeoutStopSec` to `[task_caps].max_duration_s_per_task`, the longest
a task may run, and to `infinity` when the cap is 0. A unit installed
before that change waits 14400 s whatever the cap. The timeout does not
cover a task whose `max_duration_s_override` is above the queue's cap, or
the time the pre- and post-dispatch hooks take.

**Steps:**
1. `systemctl --user show -p TimeoutStopUSec claude-task-runner` shows
   what the next stop waits.
2. After changing the cap, re-run `claude-task-runner install`. It rewrites
   the unit and reloads systemd, and the next stop waits the new timeout.
   The running supervisor needs no restart.
3. With a cap of 0, a stop waits until every in-flight task finishes. To
   end one sooner, kill only the supervisor; its tasks are dispatched again
   by the next one:

   ```sh
   systemctl --user kill --kill-whom=main --signal=KILL claude-task-runner
   ```

   With `[supervisor].adopt_workers` on, the default, a stop does not wait
   for tasks: the workers keep running and the next supervisor adopts them.

## Cron watchdog installed, but the supervisor stays down

**Symptom:** `crontab -l` shows the `# BEGIN claude_task_runner` block,
yet a stopped supervisor never comes back. Run in the queue directory,
`claude-task-runner doctor` warns under `watchdog_installed` that the
registry does not list the queue. With an empty registry, every tick in
`~/.claude_task_runner/watchdog.log` logs
`watchdog: no queues registered; nothing to do`. If the ticks log
`verdict=locked` instead, see
[Cron watchdog logs `verdict=locked`](#cron-watchdog-logs-verdictlocked).

**Cause:** a tick manages only the queue registered in
`~/.claude_task_runner/queues.json`. An older `install` did not register
its queue there, so the cron watchdog it installed has nothing to manage.

**Steps:**
1. `claude-task-runner watchdog queues` lists the registered queues. Here
   it prints nothing.
2. Register the queue:

   ```sh
   claude-task-runner watchdog register --queue <queue>
   ```

   Re-running `claude-task-runner install --queue <queue>` also works,
   because `install` now registers its queue.
3. The next tick, within a minute, starts the supervisor. `watchdog.log`
   shows `verdict=restart`, then `spawned supervisor`, and
   `claude-task-runner supervisor status` shows it alive.

## Cron watchdog logs `verdict=locked`

**Symptom:** the supervisor of the queue that the cron watchdog manages stays
down, and every tick in `~/.claude_task_runner/watchdog.log` logs
`verdict=locked detail='another supervisor (pid <pid>) holds global.lock;
starting none until it exits'`.

**Cause:** only one supervisor runs per user. Each one takes the per-user
`~/.claude_task_runner/global.lock`, and a second one exits with
`another supervisor is already running`. While another supervisor holds the
lock, a tick starts none, and counts no restart toward its crash-loop
backoff, so the managed queue's supervisor starts on the first tick after
that one exits. Usually the holder is the supervisor of the queue registered
before. `claude-task-runner install --queue <queue>` and
`claude-task-runner watchdog register --queue <queue>` replace the
registered queue, since the watchdog manages one, but they do not stop the
old queue's supervisor; both say so when they replace a queue whose
supervisor holds the lock. The holder can also be a supervisor started by
hand, or the systemd unit's.

**Steps:**
1. `ps -o args= -p <pid>` shows the holder's `--queue`.
2. To hand over now, drain it:

   ```sh
   claude-task-runner supervisor drain --queue <old-queue>
   ```

   It stops dispatching and exits once its in-flight tasks finish, and the
   next tick after that starts the managed queue's supervisor.
   `claude-task-runner supervisor stop --queue <old-queue>` exits sooner.
   With `[supervisor].adopt_workers` on, the default, the old queue's
   in-flight workers then keep running, but nothing reaps them until a
   supervisor runs for that queue again. That supervisor records each
   worker that finished in the meantime from its log.
3. If the holder is the systemd unit's supervisor, both the unit and the
   cron watchdog are installed. `claude-task-runner install uninstall`
   offers to remove each; keep the one you want.

## A registered queue was deleted or moved

**Symptom:** every minute, `~/.claude_task_runner/watchdog.log` gets a line
`watchdog: ERROR queue=<path> is not an existing directory, so its supervisor
was not restarted and the directory was not created`.
`claude-task-runner watchdog queues` prints a warning about the same path on
stderr. With the cron watchdog installed, `claude-task-runner doctor` warns
under `watchdog_installed` and prints the `unregister` command for the path.

**Cause:** the queue is registered with the cron watchdog (by `install` or
`watchdog register`), and its directory was later deleted, moved or replaced
by a file, or it sits on a filesystem that is not mounted. A tick skips that
path but keeps it registered, so a queue whose filesystem comes back is
managed again with no action. Before this was fixed, the tick recreated the
directory and started a supervisor on the empty queue. That supervisor held
the per-user `global.lock`, so the real queue's supervisor failed with
`another supervisor is already running`.

**Steps:**
1. `claude-task-runner watchdog queues` lists the registered queues and
   warns when the one the watchdog manages, the last, is not an existing
   directory.
2. If the queue moved, register the new path. It replaces the old one:

   ```sh
   claude-task-runner watchdog register --queue <new-path>
   ```

3. If it is gone for good, drop it:

   ```sh
   claude-task-runner watchdog unregister --queue <path>
   ```

   The directory need not exist. `unregister` prints `not registered:` and
   exits 0 when the queue is not listed. It exits 2 and leaves the file as
   it was when `queues.json` is corrupt: fix or remove the file, then
   register the queues you still want.
4. If its filesystem is not mounted, mount it. The next tick manages the
   queue again.
5. An older version may already have recreated the directory and started a
   supervisor on it, which still holds the lock. The real queue's
   `another supervisor is already running (...); pid=<pid>` error names that
   supervisor, and `ps -o args= -p <pid>` shows its `--queue`. If that is
   the recreated queue, unregister it first, since the next tick would
   otherwise restart it. Then stop it with
   `claude-task-runner supervisor stop --queue <path>`, and delete the
   directory once you have checked that it holds only an empty `todo/` and
   `.claude_task_runner/`.

## Cron watchdog logs that the queue's config does not load

**Symptom:** every minute, `~/.claude_task_runner/watchdog.log` has
`watchdog: ERROR queue=<queue> config=<toml> does not load, so its supervisor
was not checked or restarted:` followed by the error, and
`claude-task-runner watchdog tick` exits 1.

**Cause:** the managed queue's config fails to load. The TOML may not
parse, or it sets a value the schema rejects, or the config that
`install --config` or `watchdog register --config` recorded was moved or
deleted. A supervisor started with that file would fail to load it too, so
the tick leaves the supervisor alone, running or not. It does not fall
back to another config.

**Steps:**
1. The ERROR line names the file and the error.
2. Fix the file. Nothing needs re-running: the next tick loads it again.
   To check at once, run `claude-task-runner watchdog tick --dry-run`. It
   loads the same file, decides, starts nothing and saves no state, and it
   exits 0 once the file loads.
3. If a recorded config moved, record its new path:

   ```sh
   claude-task-runner watchdog register --queue <queue> --config <toml>
   ```

   Without `--config`, `register` drops the recorded config, and ticks go
   back to `<queue>/claude_runner.toml`. `register` refuses a config that
   does not load.

## Task worktrees filling the disk

**Symptom:** the repository's `.claude/worktrees/` holds hundreds of
directories and the disk is filling up. A queue whose pre-dispatch hook
creates one git worktree per task (ADR-0013) never removes them on its own.
On 2026-09-25 the nlmixr2lib queue had 305 of them, holding 36 GB.

**Steps (ADR-0034):**
1. Dry run. It fetches `<remote>/<parent_branch>` but removes nothing:

   ```sh
   claude-task-runner worktree reclaim --queue <queue>
   ```

   Each worktree whose task YAML names it gets one line: `would` (removable),
   `keep` with the condition it failed, or `FAIL`. The last line counts them.
2. Read the `keep` lines before applying:
   - `unmerged`: the branch is not in `origin/main` yet. Consolidate it
     first (`/runner-merge-claude-branches`).
   - `dirty`: uncommitted work. Look at it by hand; the reclaim never
     discards it.
   - `status`: the task is not `completed`. A sidecar, resume or retry
     still needs the directory.
   - `in_flight`: a dispatch thread still holds the task; try again later.
3. Apply, optionally in batches:

   ```sh
   claude-task-runner worktree reclaim --queue <queue> --apply --limit 50
   ```

   The command exits 1 when a fetch, a probe or a removal failed, and 2 when
   it could not run at all. A summary ending in `branch(es) kept by git
   branch -d` means the worktree is gone but git refused to delete a local
   branch that is not merged into its upstream. Check it with
   `git branch -vv`; the reclaim never uses `-D`.
4. To keep the count bounded from now on, add this to
   `<queue>/claude_runner.toml` and send the supervisor SIGHUP (it re-reads
   the file on its next tick) or restart it:

   ```toml
   [worktree_reclaim]
   periodic  = true
   lock_file = ".run/setup_worktree.lock"   # the flock the pre-dispatch hook takes
   ```

   Each pass removes at most `max_per_pass` worktrees, so clear a large
   backlog with the CLI first. Task YAMLs are read from `todo/` and `done/`
   (`[worktree_reclaim].task_dirs`). A task whose YAML has moved anywhere else
   is invisible to the runner: add that directory to `task_dirs`, or remove
   its worktree by hand.
