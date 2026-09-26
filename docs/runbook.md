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

## Supervisor crashed repeatedly (watchdog crash-loop)

**Symptom:** `~/.claude_task_runner/watchdog.log` shows multiple restarts
in a short window. `<queue>/.claude_task_runner/supervisor.log` ends with the
same exception each time.

This section covers the cron watchdog. Under the systemd unit, systemd
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
1. Watchdog backoff should have engaged after `crash_loop_threshold`
   crashes — verify in `watchdog.log` that exponential backoff is active.
2. Read `supervisor.log` for the failing exception. Common causes:
   - Disk full → `usage_captures/` rotation hadn't run; clear old captures.
   - Settings TOML invalid → `claude-task-runner doctor` (loads the TOML
     through the schema and reports the offending field).
   - Stale `global.lock` from a hard kill → check `~/.claude_task_runner/global.lock`,
     remove if no live process.
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

## Cron / systemd watchdog not installed

**Symptom:** Supervisor died once (e.g., after a reboot) and didn't
come back.

**Steps:**
1. `claude-task-runner doctor` shows whether watchdog is installed.
2. `claude-task-runner install` auto-detects systemd vs cron, shows the
   proposed change, asks for confirmation. Accept it.
3. Verify. Under cron, `claude-task-runner watchdog queues` must list the
   queue (see the next section if it does not). Stop the supervisor with
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
   default, the new supervisor adopts them. systemd also logs
   `Found left-over process` for each one, which is expected.

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

## Cron watchdog installed, but the supervisor stays down

**Symptom:** `crontab -l` shows the `# BEGIN claude_task_runner` block,
yet a stopped supervisor never comes back. Run in the queue directory,
`claude-task-runner doctor` warns under `watchdog_installed` that the
registry does not list the queue. With an empty registry, every tick in
`~/.claude_task_runner/watchdog.log` logs
`watchdog: no queues registered; nothing to do`.

**Cause:** a tick manages only the queues listed in
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

## A registered queue was deleted or moved

**Symptom:** every minute, `~/.claude_task_runner/watchdog.log` gets a line
`watchdog: ERROR queue=<path> is not an existing directory, so its supervisor
was not restarted and the directory was not created`.
`claude-task-runner watchdog queues` prints a warning about the same path on
stderr. With the cron watchdog installed, `claude-task-runner doctor` warns
under `watchdog_installed` and prints the `unregister` command for each such
path.

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
   warns about each one that is not an existing directory.
2. If the queue moved, register the new path and drop the old one:

   ```sh
   claude-task-runner watchdog register --queue <new-path>
   claude-task-runner watchdog unregister --queue <old-path>
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
   backlog with the CLI first. A task whose YAML has left `todo/` is invisible
   to the runner. Remove its worktree by hand, or reclaim before moving YAMLs.
