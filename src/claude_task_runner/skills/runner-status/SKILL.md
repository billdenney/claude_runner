---
name: runner-status
description: |
  Use this skill when the user asks about the Claude task runner's
  status, queue health, or what's currently running. Examples: "is
  the runner alive?", "what's in flight?", "/runner-status", "queue
  status", "what's the supervisor doing?". Replaces the older
  /queue-tend skill — same use case, new implementation. Surfaces:
  supervisor liveness, current state machine vertex, 5h + weekly
  utilization, pending count, in-flight count, awaiting-sidecar tasks,
  recent failures, parser drift state.
---

# /runner-status — task runner health snapshot

This skill is mechanical: it shells out to `claude-task-runner` to
gather state, then presents a prioritized summary so the user sees
the most-important condition first.

## Steps

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

   2. **Weekly cap** — if `snapshot.last_weekly_util_pct >= 90`:
      print "Weekly utilization NN% (paused)" in yellow. Mention
      whether end-of-week-push state is active
      (`snapshot.state == "end_of_week_push"`).

   3. **5h throttle** — if `snapshot.state in ("throttled_5h",
      "slowing_down")`: print 5h utilization + state.

   4. **Awaiting sidecars** — run
      `claude-task-runner queue states --status awaiting_sidecar
      --queue <CWD> --json` and report count + task IDs. Suggest
      `/runner-answer-sidecar` if any.

   5. **Hung tasks** — run with `--status possibly_hung`. Report each.

   6. **Recent failures** — `--status failed` and
      `--status failed_circuit_breaker`. Report counts; if non-zero,
      offer the task IDs.

   7. **Healthy summary** — if none of the above, one terse line:
      "Supervisor (state) · 5h NN% · weekly NN% · pending K ·
      in-flight M".

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
- `claude-task-runner install` if no watchdog is configured.
- `claude-task-runner supervisor start` to manually start in
  foreground.
