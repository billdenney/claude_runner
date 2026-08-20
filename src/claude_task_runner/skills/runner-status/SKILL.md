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

This skill is mechanical: it shells out to `claude-task-runner` plus
some `ps` / file-glob queries to gather state, then presents a
prioritized summary so the user sees the most-important condition
first.

## Single-command form

For a comprehensive, consistent snapshot in one shell call:

```bash
bash /home/bill/.claude/skills/runner-status/snapshot.sh --queue <CWD>
```

The bundled `snapshot.sh` produces a markdown block with: supervisor
process liveness (PID + etime + cmd), `supervisor.json` fields
(state / 5h / weekly / in_flight / since / scheduled_wakeup / drift),
state-file status breakdown (completed / failed / running /
awaiting_sidecar / possibly_hung / failed_circuit_breaker), todo/
count, open-sidecar list (task_id + sequence + the outstanding
question ids), and a **per-account
state table** sourced from supervisor.json's v3 `accounts` map
(state, 5h/weekly util, paused, in-flight count, reset + wakeup
times, last-capture timestamp). Multi-account queues see one row
per configured `[[accounts]]` block; single-account queues see a
single `default` row mirrored from the top-level fields.

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

   2. **Weekly cap** — if `snapshot.state == "throttled_weekly"`:
      print "Weekly utilization NN% > target (throttled)" in yellow.
      The trace-following rule (ADR-0022) means the supervisor will
      auto-resume when the curve catches up; surface
      `snapshot.scheduled_wakeup_at` if present.

   3. **5h throttle** — if `snapshot.state in ("throttled_5h",
      "slowing_down")`: print 5h utilization + state.

   4. **Awaiting sidecars** — run
      `claude-task-runner queue states --status awaiting_sidecar
      --queue <CWD> --json` and report count + task IDs. Suggest
      `/runner-answer-sidecar` if any.

      Report the **question** count alongside the request count: sidecars
      are open per question (ADR-0031), so one request can owe four
      answers, and a request whose response answered only some of its
      questions is still open. `sidecar list --json` carries both as
      `n_open` and `n_outstanding_questions`. Never report "0 open
      sidecars" off the request count alone.

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
