#!/bin/bash
# One-pass status snapshot for the Claude task runner.
#
# Usage:
#   ./snapshot.sh [--queue <queue_dir>]
#
# Output: a markdown block with supervisor liveness, supervisor.json
# state, state-file count, open-sidecar list, queue counts, and live
# `claude-task-runner usage render` output. Default queue is $PWD.
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

# ----- Live usage -----
if command -v claude-task-runner > /dev/null 2>&1; then
  echo "**Live usage** (\`claude-task-runner usage render\`)"
  echo '```'
  claude-task-runner usage render 2>&1 | head -8 || echo "(render failed)"
  echo '```'
fi
