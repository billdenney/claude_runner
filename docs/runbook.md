# Operator runbook

Quick reference for common situations. Each scenario links to the
relevant component / ADR for deeper context.

## Parser drift detected

**Symptom:** `claude-task-runner usage healthcheck` returns non-zero.
Supervisor is in `ErrorDrift`. `drift.log` has recent entries. Desktop
notification fired.

**Causes:** Anthropic changed the `/usage` TUI layout. Most common forms:
field rename (`Resets` → `Resets at`), new section added, ANSI escape
sequence variant.

**Steps:**
1. `claude-task-runner usage capture --save /tmp/drift-$(date +%s).cap`
   to record fresh raw output.
2. `claude-task-runner usage parse-file /tmp/drift-*.cap` to see exact
   parse failure.
3. Inspect the `.cap` (cat with ANSI rendering) to identify what changed.
4. Update `usage/parser.py` state machine (or add a fixture variant).
5. Run `pytest tests/unit/test_parser.py` until green.
6. Commit fixture under `tests/fixtures/usage/<date>_<reason>.cap` so the
   regression is locked in.
7. Save the fix-validating fixture as `*.keep` to exempt from rotation.
8. Restart supervisor. After 3 consecutive clean polls, it leaves
   `ErrorDrift`.

## Supervisor crashed repeatedly (watchdog crash-loop)

**Symptom:** `watchdog.log` shows multiple restarts in a short window.
`supervisor.log` ends with the same exception each time.

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
N hours old. EMA suggests it should have completed by now.

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
3. Verify: kill the supervisor manually; within ~60s (cron) or ~30s
   (systemd) it should restart. Check `watchdog.log`.

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
