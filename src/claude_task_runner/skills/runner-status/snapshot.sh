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
# times, last capture). Default queue is $PWD.
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
while [[ $# -gt 0 ]]; do
  case "$1" in
    --queue) QUEUE="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

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

# ----- State-file & queue counts -----
STATE_DIR="$QUEUE/.claude_task_runner/state"
if [[ -d "$STATE_DIR" ]]; then
  STATE_COUNT="$(find "$STATE_DIR" -maxdepth 1 -name '*.yaml' -type f 2>/dev/null | wc -l)"
else
  STATE_COUNT=0
fi

TODO_DIR="$QUEUE/todo"
if [[ -d "$TODO_DIR" ]]; then
  TODO_COUNT="$(find "$TODO_DIR" -maxdepth 1 -name '*.yaml' -type f 2>/dev/null | wc -l)"
else
  TODO_COUNT=0
fi

# Status breakdown across state YAMLs (completed / failed / running / etc.)
if [[ -d "$STATE_DIR" ]]; then
python3 - "$STATE_DIR" <<'PY'
import os, sys, glob, re
from collections import Counter
state_dir = sys.argv[1]
counts = Counter()
status_re = re.compile(r"^status:\s*(\S+)\s*$", re.M)
for p in glob.glob(os.path.join(state_dir, "*.yaml")):
    try:
        with open(p) as f:
            text = f.read(2000)
        m = status_re.search(text)
        if m:
            counts[m.group(1)] += 1
    except Exception:
        counts["read_error"] += 1
print("**Queue counts**")
print()
print("| field | value |")
print("|---|---|")
for k in ("completed","failed","running","awaiting_sidecar","possibly_hung","failed_circuit_breaker"):
    print(f"| state.{k} | {counts.get(k,0)} |")
total_state = sum(counts.values())
print(f"| state files (total) | {total_state} |")
PY
fi
echo "| todo/*.yaml | $TODO_COUNT |"
echo ""

# ----- Open sidecars -----
if command -v claude-task-runner > /dev/null 2>&1; then
  SC_FILE="$(mktemp)"
  trap 'rm -f "$SC_FILE"' EXIT
  claude-task-runner sidecar list --queue "$QUEUE" --json > "$SC_FILE" 2>/dev/null || echo '{"sidecars":[]}' > "$SC_FILE"
  SC_FILE="$SC_FILE" python3 - <<'PY'
import json
import os
with open(os.environ["SC_FILE"]) as f:
    d = json.load(f)
n = len(d.get("sidecars", []))
print(f"**Open sidecars**: {n}")
print()
if n == 0:
    print("(none)")
else:
    print("| task_id | sequence |")
    print("|---|---|")
    for s in d["sidecars"]:
        print(f"| {s['task_id']} | {s.get('sequence','?')} |")
PY
else
  echo "**Open sidecars**: claude-task-runner not on PATH"
fi
echo ""

# ----- Per-account state -----
#
# Source the v3 supervisor.json's `accounts` map (one entry per
# configured [[accounts]] block, populated by the multi-account
# /usage capture round-robin from PR 8). Reports each account's
# state, 5h util, weekly util, paused flag, per-account in-flight
# count (derived from supervisor.json `in_flight` records'
# `account` attribution), 5h + weekly reset, scheduled wakeup,
# and last `/usage` capture timestamp.
#
# Expects supervisor.json v3 (`schema_version: 3`). v2 files are
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
    "| account | state | 5h | weekly | paused | in-flight | "
    "5h reset | weekly reset | wakeup | last capture |"
)
print("|---|---|---:|---:|:-:|---:|---|---|---|---|")
for name in sorted(accounts):
    a = accounts[name]
    paused = "yes" if a.get("paused") else ""
    last_cap = a.get("last_capture_at") or "—"
    wakeup = a.get("scheduled_wakeup_at") or "—"
    print(
        f"| {name} | `{a.get('state','?')}` "
        f"| {a.get('last_5h_util_pct','?')}% "
        f"| {a.get('last_weekly_util_pct','?')}% "
        f"| {paused} "
        f"| {in_flight_by_account.get(name, 0)} "
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
