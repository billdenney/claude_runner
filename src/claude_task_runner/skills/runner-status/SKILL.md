---
name: runner-status
description: |
  Use this skill when the user asks about the Claude task runner's
  status, queue health, or what's currently running. Examples: "is
  the runner alive?", "what's in flight?", "/runner-status", "queue
  status", "what's the supervisor doing?". Replaces the older
  /queue-tend skill — same use case, new implementation. Leads with a
  plain-language explanation anyone can read, then the details:
  supervisor liveness, what each account is doing, the queue's
  concurrency ceiling, broken account settings files, 5h + weekly
  utilization, task counts, open questions, the last "no eligible
  tasks" report, recent failures, parser drift state.
---

# /runner-status — task runner health snapshot

This skill is mechanical: it shells out to `claude-task-runner` plus
some `ps` / file-glob queries to gather state, then presents a
prioritized summary so the user sees the most-important condition
first.

## Single-command form

For a comprehensive, consistent snapshot in one shell call:

```bash
bash /home/bill/.claude/skills/runner-status/snapshot.sh --queue <CWD>
```

The bundled `snapshot.sh` prints, first, **In plain words**: a short
summary a non-specialist can read in ten seconds, then a list headed
**Not normal right now** with one sentence per abnormal condition saying
what it means and what to do (or "Everything looks normal."). It covers:

- whether the supervisor is running: its PID is alive and
  `supervisor.json`, which it rewrites every tick, was saved within three
  `[usage].poll_interval_s`;
- what each account is doing (taking tasks, with slots in use out of the
  slots it is allowed now; or not taking tasks, and why: paused, a limit
  reached with the time it starts again, no usage reading, a broken
  settings file);
- the concurrency ceiling: how many tasks the queue may run at once, where
  the number comes from (the sum of the accounts' own `max_concurrency`,
  or a queue-wide `[concurrency].max_concurrency` that binds below it),
  and how many may run right now;
- broken account settings files (`runner-account.toml` that does not
  load): the account takes no tasks until the file is fixed;
- task counts in plain words, and the questions waiting for an answer;
- when nothing is running while an account has room: the supervisor's last
  `no eligible tasks:` journal line, in words, with its time.

Under **Details** come the sections the summary is built from, each opening
with a line saying what it shows and defining its terms:

- **Supervisor**: process liveness (PID + etime + cmd).
- **supervisor.json**: state / 5h / weekly / in_flight / since /
  scheduled_wakeup / drift, and `last saved` (the file's modification
  time).
- **Accounts and concurrency**, from `claude-task-runner account list
  --json`: each account's own limit, how many it is allowed now, in flight,
  paused, its settings file and any error in it; then the queue ceiling
  with its parts (the accounts' limits, the queue-wide cap and whether it
  binds, the warm-up `initial_concurrency`, broken account files).
- **Per-account state**, from supervisor.json's `accounts` map: state,
  5h/weekly util, paused (read from the `account_paused/` markers, which
  `supervisor.json` trails by a tick), in-flight count, throttle target,
  reset + wakeup times, last-capture timestamp. The target is the most
  tasks dispatch lets the account run (below its `max_concurrency`): the
  ADR-0022 ramp while `slowing_down`, 0 while throttled, "—" when no
  decision has set one.
- **Queue counts**: a row for every task status, one for any status the
  runner does not know, and one for files it could not read or that name
  no status (the rows add up to the total), and the todo/ count.
- **Last idle report**: the newest `no eligible tasks:` or `eligible tasks
  available again:` line the running supervisor logged (journal, or
  `<queue>/.claude_task_runner/supervisor.log` under cron), with its time;
  a line from before the supervisor started is shown as such and not used.
- **Open sidecars**: task_id + sequence + the outstanding question ids.

## How to reply

1. **Lead with the plain-language explanation.** Start from the script's
   **In plain words** block and say it in your own plain words: no jargon,
   and define any term you must use the first time it appears (for
   example "in flight: running now"; "throttled: the account has used
   enough of its usage allowance that it pauses until the window resets";
   "awaiting_sidecar: the task stopped to ask you a question"; "deferred:
   on hold until a file appears, a retry time passes or a setting is
   fixed"; "concurrency ceiling: the most tasks the queue may run at once").
   Give every **Not normal right now** item, each with what to do.
2. **Then the details**, section by section, each with one sentence on
   what it means, including the concurrency ceiling (its value, where it
   comes from, whether the queue-wide cap binds) and any broken account
   settings file (which file, the error, and that the account takes tasks
   again within a tick of the fix).
3. If a section could not be gathered, say its facts are unknown; never
   describe them as empty or healthy.

The script's exit code says whether the report can be trusted:

- **2, no report:** the queue does not exist or has no `todo/`
  subdirectory, usually a mistyped `--queue` or a working directory
  that is not the queue. The reason and the path are on stderr. Tell
  the user which path was tried and ask for the queue's path. Never
  describe it as an idle or empty queue.
- **1, report printed:** the open sidecars could not be listed. The
  **Open sidecars** line reads "could not list" and gives the reason.
  Say the open sidecars are unknown, never "no open sidecars".
- **0:** every section of the report was gathered, except possibly the
  accounts: when `account list` fails, the **Accounts and concurrency**
  section reads "could not list" and the summary says the accounts could
  not be read. Say the accounts and the ceiling are unknown then.

This is the **default invocation** when the user says
`/runner-status` or "queue status" — produces the same output shape
every time so snapshots can be compared across time.

The per-account table replaces an earlier `claude-task-runner usage
render` block that captured a fresh `/usage` reading for one account
per call. The supervisor's per-account snapshot is at most one
`poll_interval_s` old (typically 30-60s), shows every account, and
costs no API tokens — better default for status checks. Operators
who want a current `/usage` capture can still run
`claude-task-runner usage render` directly.

If the user wants a deeper investigation (e.g. focused on hung tasks
or recent failures), follow the prioritized triage flow below.

## Steps (priority-triage form)

1. **Run** `claude-task-runner supervisor status --queue <CWD> --json`
   to get the supervisor snapshot, alive flag, and counts. The user's
   working directory is the default queue; use `--queue <PATH>` if
   they referenced a different one.

2. **Parse** the JSON. Surface findings in this priority order
   (highest first — only show every line that is relevant):

   1. **Parser drift** — if `snapshot.last_drift_message` is non-empty
      OR `snapshot.state == "error_drift"`: this is critical. Print a
      red line with the message. Suggest the user run
      `claude-task-runner usage healthcheck` or invoke `/runner-usage`
      to investigate. Stop here unless the user wants more.

   2. **Broken account settings file** — if `account_errors` is
      non-empty: each entry is an account whose `runner-account.toml`
      does not load. That account silently takes no tasks and counts 0
      toward the ceiling, while every other account runs as usual, so
      nothing else looks wrong. Give the account, the file (`path`) and
      the `error`, and say: fix the file; the account takes tasks again
      at the supervisor's next tick. `snapshot.accounts.<name>.policy_error`
      carries the same error.

   3. **No usage reading** — if `snapshot.state == "no_reading"`, or
      any entry of `snapshot.accounts` has `state == "no_reading"`:
      that account takes no tasks until a clean `/usage` capture lands.
      Print each such account with its `last_reading_at` ("never" when
      null) in yellow. Every account starts there after a supervisor
      start and leaves within one capture cycle (one poll per account),
      so only one that persists is a problem; point the user at the
      runbook section "An account stays in `no_reading`".

   4. **Weekly cap** — if `snapshot.state == "throttled_weekly"`:
      print "Weekly utilization NN% > target (throttled)" in yellow.
      The trace-following rule (ADR-0022) means the supervisor will
      auto-resume when the curve catches up; surface
      `snapshot.scheduled_wakeup_at` if present.

   5. **5h throttle** — if `snapshot.state in ("throttled_5h",
      "slowing_down")`: print 5h utilization + state. For each
      `slowing_down` account, also give its target from the
      per-account table ("personal slowing down, target 2").

   6. **Queue-wide cap binds** — if `queue_ceiling.binds` is true: the
      queue TOML's `[concurrency].max_concurrency` (`queue_ceiling.
      max_concurrency`) is below the sum of the accounts' own caps
      (`queue_ceiling.accounts_total`, broken down in
      `queue_ceiling.account_caps`), so it, not the accounts, limits how
      many tasks run. Say so in plain words and that raising or removing
      it uses every account slot. Otherwise give the ceiling in one line
      ("may run 8 at once: personal 5 + work 3, no queue-wide cap").

   7. **Awaiting sidecars** — run
      `claude-task-runner queue states --status awaiting_sidecar
      --queue <CWD> --json` and report count + task IDs. Suggest
      `/runner-answer-sidecar` if any.

      Report the **question** count alongside the request count: sidecars
      are open per question (ADR-0031), so one request can owe four
      answers, and a request whose response answered only some of its
      questions is still open. `sidecar list --json` carries both as
      `n_open` and `n_outstanding_questions`. Never report "0 open
      sidecars" off the request count alone.

   8. **Hung tasks** — run with `--status possibly_hung`. Report each.

   9. **Recent failures** — `--status failed` and
      `--status failed_circuit_breaker`. Report counts; if non-zero,
      offer the task IDs.

   10. **Deferred tasks** — run `claude-task-runner queue states
       --status deferred --queue <CWD> --json` and report the count,
       grouped by how each state's `deferred_reason` starts:

       - `readiness hold:` — waiting on an unmet readiness requirement,
         such as a file or a sidecar response (ADR-0030). The supervisor
         checks it every tick and un-parks the task the first tick after
         the requirement is met. Give the count and offer the task IDs.
       - `invalid effort:` — the task's `(model, effort)` pair is not in
         `[effort_levels]` (ADR-0010). It stays parked until the task
         YAML is fixed, or the pair is added to the TOML and the
         supervisor gets SIGHUP. This needs the operator: list each task
         with its reason.
       - `pre-dispatch hook deferred (exit 1)` — the queue's pre-dispatch
         hook asked to wait. The hook runs again once the task's
         `next_eligible_at` passes, which is `deferral_recheck_cooldown_s`
         in `[failure_classifier]` (900 s by default) after the deferral.
         Of the runner's own deferrals, only these set
         `next_eligible_at`. Give the count and the earliest
         `next_eligible_at`.
       - Anything else was written by hand or by another tool. The
         reason is only a label and does not hold the task. Once its
         `next_eligible_at` passes, or at the next tick if it has none,
         the task can be dispatched again. If a gate holds it then, the
         gate's own reason replaces the hand-written one. List each task
         with its reason and `next_eligible_at`.

       A row with an `error` field is a state file that could not be
       parsed. `queue states` lists those whatever `--status` asks for,
       so report them separately, not as deferred.

   11. **Nothing in flight** — if no task is in flight and some account
       is open for dispatch, read the snapshot's **Last idle report**: the
       newest line about eligible tasks the running supervisor logged
       (its journal entries under systemd,
       `<queue>/.claude_task_runner/supervisor.log` under cron). A
       `no eligible tasks: N unfinished held (...)` line counts the
       unfinished tasks by what holds them (`block_dispatch`,
       `readiness_missing_file`, `depends_on`, a status name, ...). It
       says the queue was drained as of that line's timestamp, not that
       the supervisor is ticking now: it is logged only when the counts
       change, so it may be days old. Confirm liveness separately (the
       summary's first sentence: the PID is alive and `supervisor.json`,
       which the supervisor rewrites every tick, was saved recently)
       before reporting the counts as a drained queue. If the last line is
       `eligible tasks available again` instead, the queue had work at
       that tick. The runbook section "Nothing dispatches, but an account
       has room" explains each count.

   12. **Healthy summary** — if none of the above, one terse line:
       "Supervisor (state) · 5h NN% · weekly NN% · pending K ·
       in-flight M · ceiling C".

3. **Don't auto-fix anything.** This skill is read-only. If the user
   wants action (resume a failed task, answer a sidecar, restart the
   supervisor), name the right next skill / command but wait for
   confirmation.

## Things this skill does NOT do

- Doesn't restart the supervisor. (User can do that with
  `claude-task-runner supervisor start`, or rely on the
  watchdog if installed.)
- Doesn't dispatch new tasks.
- Doesn't modify state files.

## When the supervisor isn't alive

If `supervisor_alive == false`, that's not always bad — the watchdog
may be about to restart it. Mention liveness, mention whether a PID
file exists (stale vs missing), then suggest:

- `claude-task-runner watchdog tick` to trigger an immediate watchdog
  evaluation.
- `claude-task-runner watchdog queues` to check that a cron watchdog
  manages this queue. It manages one queue, the last line printed; if
  that is not this one, `claude-task-runner watchdog register --queue <queue>`
  makes it so, replacing the other. It warns on stderr about any other
  queue listed, which the watchdog ignores, and when the managed queue
  is no longer an existing directory, which every tick skips; drop that
  one with `claude-task-runner watchdog unregister --queue <path>`.
  `verdict=locked` in `~/.claude_task_runner/watchdog.log` means another
  supervisor holds the per-user lock, so no tick starts this queue's
  supervisor until that one exits.
- `claude-task-runner install` if no watchdog is configured.
- `claude-task-runner supervisor start` to manually start in
  foreground.
