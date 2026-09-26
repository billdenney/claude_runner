#!/bin/bash
# One-pass status snapshot for the Claude task runner.
#
# Usage:
#   ./snapshot.sh [--queue <queue_dir>]
#
# Output: a markdown block with supervisor liveness, supervisor.json
# state, state-file count, open-sidecar list, queue counts, and a
# per-account state table sourced from supervisor.json's v3 `accounts`
# map (state, 5h/weekly util, paused, in-flight count, reset + wakeup
# times, last capture). Default queue is $PWD. The queue must be an
# existing directory with a todo/ subdirectory.
#
# Exit codes:
#   0  report printed
#   1  report printed, but the open sidecars could not be listed; the
#      "Open sidecars" line says "could not list" and why
#   2  bad args, or the queue is missing or not a queue; no report
#
# Note: the per-account section replaces the older
# `claude-task-runner usage render` block, which only captured one
# account's live `/usage` reading and was misleading on multi-account
# queues. Operators who want a fresh `/usage` capture can still run
# `claude-task-runner usage render` directly.
#
# Designed to be invoked as the body of /runner-status; produces the
# same shape every time so a user can diff snapshots over time.

set -euo pipefail

QUEUE="${PWD}"
QUEUE_FROM="the working directory (no --queue given)"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --queue) QUEUE="$2"; QUEUE_FROM="--queue"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Refuse anything that is not a queue before reporting on it. Every section
# below reports what it finds under $QUEUE, so a mistyped --queue, or a run
# from the wrong directory without one, used to print an idle, empty queue
# (supervisor NOT RUNNING, supervisor.json missing, 0 todo, no open sidecars)
# and exit 0. A queue is a directory with a todo/ subdirectory, the test
# `worktree reclaim` applies too.
if [[ ! -d "$QUEUE" ]]; then
  echo "$QUEUE_FROM is not an existing directory: $QUEUE" >&2
  exit 2
fi
if [[ ! -d "$QUEUE/todo" ]]; then
  echo "$QUEUE_FROM is not a queue directory, it has no todo/ subdirectory: $QUEUE" >&2
  exit 2
fi

NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "## Queue status — $NOW"
echo ""

# ----- Supervisor process liveness -----
PIDFILE="$QUEUE/.claude_task_runner/supervisor.pid"
if [[ -f "$PIDFILE" ]]; then
  PID="$(cat "$PIDFILE")"
  if [[ -n "$PID" ]] && ps -p "$PID" > /dev/null 2>&1; then
    PSLINE="$(ps -p "$PID" -o pid,etime,time,cmd= --no-headers 2>/dev/null || true)"
    echo "**Supervisor**: alive PID $PID"
    echo '```'
    echo "$PSLINE"
    echo '```'
  else
    echo "**Supervisor**: NOT RUNNING (stale pidfile $PIDFILE → $PID)"
  fi
else
  echo "**Supervisor**: NOT RUNNING (no pidfile at $PIDFILE)"
fi
echo ""

# ----- supervisor.json state -----
SUP_JSON="$QUEUE/.claude_task_runner/supervisor.json"
if [[ -f "$SUP_JSON" ]]; then
  python3 - "$SUP_JSON" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
fields = [
    ("state", d.get("state")),
    ("5h util", f'{d.get("last_5h_util_pct","?")}%'),
    ("weekly util", f'{d.get("last_weekly_util_pct","?")}%'),
    ("in_flight", len(d.get("in_flight_task_ids") or [])),
    ("since", d.get("since","?")),
    ("scheduled_wakeup", d.get("scheduled_wakeup_at","-")),
    ("5h reset_at", d.get("last_5h_reset_at","?")),
    ("weekly reset_at", d.get("last_weekly_reset_at","?")),
    ("drift", d.get("last_drift_message","") or "-"),
]
print("**supervisor.json**")
print()
print("| field | value |")
print("|---|---|")
for k, v in fields:
    print(f"| {k} | `{v}` |")
PY
else
  echo "**supervisor.json**: missing at $SUP_JSON"
fi
echo ""

# ----- Per-account state -----
#
# Source the v3 supervisor.json's `accounts` map (one entry per
# configured [[accounts]] block, populated by the multi-account
# /usage capture round-robin from PR 8). Reports each account's
# state, 5h util, weekly util, paused flag, per-account in-flight
# count (derived from supervisor.json `in_flight` records'
# `account` attribution), the throttle target that caps it
# (`target_concurrency`: the ADR-0022 ramp while slowing_down; "—"
# when no decision has set one), 5h + weekly reset, scheduled wakeup,
# and last `/usage` capture timestamp.
#
# Expects supervisor.json v3 or later (`schema_version` >= 3). v2 files are
# auto-migrated by the persistence layer at daemon load time. If
# this section reports "(no accounts map)", the file is either v2
# (start the supervisor once to migrate) or a brand-new snapshot
# that hasn't been ticked yet (supervisor.json was written but
# `initial_snapshot` hadn't populated `accounts` for some reason).
# Either way, we soft-fail with a marker line so the rest of the
# script's epilogue (if any future sections are added) continues.
if [[ -f "$SUP_JSON" ]]; then
python3 - "$SUP_JSON" <<'PY'
import json
import sys
from collections import Counter

with open(sys.argv[1]) as f:
    d = json.load(f)
schema_version = d.get("schema_version")
accounts = d.get("accounts") or {}
if not accounts:
    print("**Per-account state**")
    print()
    print(
        "_no `accounts` map in supervisor.json "
        f"(schema_version={schema_version!r}); v2 files are "
        "auto-migrated on next supervisor start. If this is a "
        "v3 snapshot, the supervisor has not completed a tick "
        "yet — the `accounts` map is populated by `initial_snapshot`._"
    )
    sys.exit(0)
# Per-account in-flight counts, derived from supervisor.json's
# attributed in_flight list (each record carries `account`).
in_flight = d.get("in_flight") or []
in_flight_by_account = Counter(
    rec.get("account") for rec in in_flight if rec.get("account")
)
print("**Per-account state** (from supervisor.json `accounts`)")
print()
print(
    "| account | state | 5h | weekly | paused | in-flight | target | "
    "5h reset | weekly reset | wakeup | last capture |"
)
print("|---|---|---:|---:|:-:|---:|---:|---|---|---|---|")
for name in sorted(accounts):
    a = accounts[name]
    paused = "yes" if a.get("paused") else ""
    last_cap = a.get("last_capture_at") or "—"
    wakeup = a.get("scheduled_wakeup_at") or "—"
    target = a.get("target_concurrency")
    print(
        f"| {name} | `{a.get('state','?')}` "
        f"| {a.get('last_5h_util_pct','?')}% "
        f"| {a.get('last_weekly_util_pct','?')}% "
        f"| {paused} "
        f"| {in_flight_by_account.get(name, 0)} "
        f"| {'—' if target is None else target} "
        f"| {a.get('last_5h_reset_at','—')} "
        f"| {a.get('last_weekly_reset_at','—')} "
        f"| {wakeup} "
        f"| {last_cap} |"
    )
# Surface any per-account drift message separately — the table
# would get unreadable if drift strings (often long, embedded
# pipes) were inlined as a column. Empty-string drift means
# healthy.
drift_rows = [
    (name, accounts[name].get("last_drift_message", ""))
    for name in sorted(accounts)
    if accounts[name].get("last_drift_message")
]
if drift_rows:
    print()
    print("_Per-account drift messages:_")
    print()
    for name, msg in drift_rows:
        # Pipe-escape so the markdown list item doesn't truncate.
        safe = msg.replace("|", r"\|")
        print(f"- `{name}`: {safe}")
PY
fi
echo ""

# ----- State-file & queue counts -----
STATE_DIR="$QUEUE/.claude_task_runner/state"
TODO_COUNT="$(find "$QUEUE/todo" -maxdepth 1 -name '*.yaml' -type f | wc -l)"

# Status breakdown across state YAMLs: a row for every task status, a row for
# each status the runner does not know, and one for files that could not be
# read or name no status, so the rows add up to the total. Pending, deferred
# and weekly_paused tasks, and unreadable files, used to be counted in the
# total and in no row.
#
# Runs without a state directory too, counting 0 of each: a queue whose
# supervisor has never started has none yet, and the table header must still
# come before the todo/*.yaml row, which used to be printed on its own.
python3 - "$STATE_DIR" <<'PY'
import glob
import os
import re
import sys
from collections import Counter

# TaskStatus in queue/schema.py, in its order. The heredoc runs under plain
# python3 and cannot import the package, so tests/unit/test_snapshot_script.py
# seeds a state file for every TaskStatus and fails until each has its row.
STATUSES = (
    "pending",
    "running",
    "awaiting_sidecar",
    "deferred",
    "possibly_hung",
    "completed",
    "failed",
    "failed_circuit_breaker",
    "weekly_paused",
)
state_dir = sys.argv[1]
counts = Counter()
status_re = re.compile(r"^status:\s*(\S+)\s*$", re.M)
for p in glob.glob(os.path.join(state_dir, "*.yaml")):
    try:
        with open(p) as f:
            text = f.read(2000)
    except (OSError, ValueError):
        text = ""
    m = status_re.search(text)
    # None counts the files that could not be read or name no status.
    counts[m.group(1) if m else None] += 1
print("**Queue counts**")
print()
print("| field | value |")
print("|---|---|")
for k in STATUSES:
    print(f"| state.{k} | {counts[k]} |")
for k in sorted(k for k in counts if k is not None and k not in STATUSES):
    print(f"| state.{k} (unknown status) | {counts[k]} |")
print(f"| state files unreadable or without a status | {counts[None]} |")
print(f"| state files (total) | {sum(counts.values())} |")
PY
echo "| todo/*.yaml | $TODO_COUNT |"
echo ""

# ----- Open sidecars -----
#
# "None open" and "could not list" must never read alike. A failed `sidecar
# list` used to be replaced with an empty listing, so this section said
# "(none)" and the script exited 0. Now a CLI missing from PATH, a non-zero
# exit, or output that is not a listing prints "could not list" and why, and
# the script exits 1 once the report is out.
SIDECARS_RC=0
if command -v claude-task-runner > /dev/null 2>&1; then
  SC_OUT="$(mktemp)"
  SC_ERR="$(mktemp)"
  trap 'rm -f "$SC_OUT" "$SC_ERR"' EXIT
  SC_RC=0
  claude-task-runner sidecar list --queue "$QUEUE" --json > "$SC_OUT" 2> "$SC_ERR" || SC_RC=$?
  SC_RC="$SC_RC" SC_OUT="$SC_OUT" SC_ERR="$SC_ERR" python3 - <<'PY' || SIDECARS_RC=$?
import json
import os
import sys


def could_not_list(why, output):
    """Say why the sidecars could not be listed, show the output's tail, exit 1."""
    print(f"**Open sidecars**: could not list: {why}")
    lines = output.strip().splitlines()
    if lines:
        print()
        print("```")
        print("\n".join(lines[-20:]))
        print("```")
    sys.exit(1)


rc = int(os.environ["SC_RC"])
with open(os.environ["SC_OUT"]) as f:
    stdout = f.read()
with open(os.environ["SC_ERR"]) as f:
    printed = stdout.rstrip("\n") + "\n" + f.read()
if rc != 0:
    could_not_list(f"`claude-task-runner sidecar list --json` exited {rc}", printed)
try:
    d = json.loads(stdout)
    rows = d["sidecars"]
except (ValueError, KeyError, TypeError) as exc:
    could_not_list(f"`claude-task-runner sidecar list --json` printed no listing ({exc!r})", printed)
n = d.get("n_open", len(rows))
# Openness is per QUESTION (ADR-0031): one request can hold several
# unanswered ids, and a request whose response answered only some of them
# is still open. Reporting requests alone understates the work owed.
nq = d.get("n_outstanding_questions")
if nq is None:
    nq = sum(len(s.get("outstanding") or []) for s in rows)
print(f"**Open sidecars**: {n} request(s), {nq} unanswered question(s)")
print()
if n == 0:
    print("(none)")
else:
    print("| task_id | sequence | outstanding | state |")
    print("|---|---|---|---|")
    for s in rows:
        out = ", ".join(s.get("outstanding") or []) or "-"
        answered = s.get("answered") or []
        if s.get("error"):
            state = "unreadable"
        elif s.get("partial") and answered:
            state = "partial (answered: %s)" % ", ".join(answered)
        elif s.get("partial"):
            # A response exists but credits none of the asked ids -- usually
            # an answer written against the wrong question id.
            state = "unmatched response"
        else:
            state = "unanswered"
        print(f"| {s['task_id']} | {s.get('sequence','?')} | {out} | {state} |")
PY
else
  echo "**Open sidecars**: could not list: claude-task-runner is not on PATH"
  SIDECARS_RC=1
fi
echo ""
if (( SIDECARS_RC != 0 )); then
  echo "could not list the open sidecars, so the report is incomplete" >&2
  exit 1
fi
