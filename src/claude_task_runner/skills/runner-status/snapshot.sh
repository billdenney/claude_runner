#!/bin/bash
# One-pass status snapshot for the Claude task runner.
#
# Usage:
#   ./snapshot.sh [--queue <queue_dir>]
#
# Output: a markdown block, written by report.py beside this script. It
# opens with a plain-language summary: whether the supervisor is running,
# what each account is doing, how many tasks can run, what holds the tasks,
# the questions waiting, then one sentence per condition that needs
# attention and one per routine condition worth knowing. The details follow,
# each with a line saying what it shows. This script prints the open
# sidecars last itself. Default queue is $PWD. The queue must be an existing
# directory with a todo/ subdirectory.
#
# The runner's facts come from its CLI, called with the supervisor's own
# --config when it was started with one: `account list --json`, `queue
# holds --json` (the supervisor's candidate selection, read-only) and
# `sidecar list --json`. If report.py fails the details it could not print
# say so, and the exit code still means only what is listed below.
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
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# ----- Gather -----
#
# Everything is gathered before anything is printed, so the summary can come
# first.
PIDFILE="$QUEUE/.claude_task_runner/supervisor.pid"
PID=""
PID_STATE="missing"
PID_ETIME=""
PSLINE=""
if [[ -f "$PIDFILE" ]]; then
  PID="$(cat "$PIDFILE")"
  if [[ -n "$PID" ]] && ps -p "$PID" > /dev/null 2>&1; then
    PID_STATE="alive"
    PID_ETIME="$(ps -p "$PID" -o etime= 2>/dev/null | tr -d ' ' || true)"
    PSLINE="$(ps -p "$PID" -o pid,etime,time,args= 2>/dev/null | tail -n 1 || true)"
  else
    PID_STATE="stale"
  fi
fi

# The --config the live supervisor was started with, so the CLI reads the
# same settings it does (its poll interval and block-list among them). Only
# the arguments after `supervisor start` count: an interpreter's own -c
# comes before them.
SUP_CONFIG=""
if [[ "$PID_STATE" == "alive" ]]; then
  if [[ -r "/proc/$PID/cmdline" ]]; then
    SUP_ARGS="$(tr '\0' '\n' < "/proc/$PID/cmdline")"
  else
    SUP_ARGS="$(ps -p "$PID" -o args= 2>/dev/null | tr ' ' '\n' || true)"
  fi
  prev=""
  after_start=0
  while IFS= read -r arg; do
    if (( after_start )); then
      case "$prev" in
        --config|-c) SUP_CONFIG="$arg" ;;
      esac
      case "$arg" in
        --config=*) SUP_CONFIG="${arg#--config=}" ;;
      esac
    elif [[ "$prev" == "supervisor" && "$arg" == "start" ]]; then
      after_start=1
    fi
    prev="$arg"
  done <<< "$SUP_ARGS"
fi
CONFIG_ARGS=()
if [[ -n "$SUP_CONFIG" ]]; then
  CONFIG_ARGS=(--config "$SUP_CONFIG")
fi

# Run `claude-task-runner <args>` into $TMP/<name>.{out,err,rc}, in the
# background; rc is "missing" when the CLI is not on PATH. `queue holds`
# reads every task and state file, so the three run side by side.
run_cli() {
  local name="$1"
  shift
  if ! command -v claude-task-runner > /dev/null 2>&1; then
    echo "missing" > "$TMP/$name.rc"
    : > "$TMP/$name.out"
    : > "$TMP/$name.err"
    return
  fi
  local rc=0
  claude-task-runner "$@" > "$TMP/$name.out" 2> "$TMP/$name.err" || rc=$?
  echo "$rc" > "$TMP/$name.rc"
}
run_cli accounts account list --queue "$QUEUE" ${CONFIG_ARGS[@]+"${CONFIG_ARGS[@]}"} --json &
run_cli holds queue holds --queue "$QUEUE" ${CONFIG_ARGS[@]+"${CONFIG_ARGS[@]}"} --json &
run_cli sidecars sidecar list --queue "$QUEUE" --json &

# The live supervisor's journal entries (systemd), found by its PID so
# another queue's supervisor, or an earlier run, is left out: whether there
# are any, and the newest line about eligible tasks. report.py also reads
# <queue>/.claude_task_runner/supervisor.log (cron watchdog).
JOURNAL_STATE="unavailable"
JOURNAL_LINE=""
if [[ "$PID_STATE" == "alive" ]] && command -v journalctl > /dev/null 2>&1; then
  if journalctl --user "_PID=$PID" -n 1 -o cat --no-pager > "$TMP/journal" 2> /dev/null; then
    if [[ -s "$TMP/journal" ]] && ! grep -qx -- '-- No entries --' "$TMP/journal"; then
      JOURNAL_STATE="read"
      JOURNAL_LINE="$(journalctl --user "_PID=$PID" -r -o cat --no-pager 2> /dev/null \
        | grep -m1 -E 'no eligible tasks:|eligible tasks available again:' || true)"
    else
      JOURNAL_STATE="empty"
    fi
  fi
fi
wait

export RS_QUEUE="$QUEUE" RS_NOW="$NOW" RS_PID_STATE="$PID_STATE" RS_PID="$PID"
export RS_PID_ETIME="$PID_ETIME" RS_PSLINE="$PSLINE"
export RS_JOURNAL_STATE="$JOURNAL_STATE" RS_JOURNAL_LINE="$JOURNAL_LINE"
for name in accounts holds sidecars; do
  upper="$(echo "$name" | tr '[:lower:]' '[:upper:]')"
  export "RS_${upper}_RC=$(cat "$TMP/$name.rc")"
  export "RS_${upper}_OUT=$TMP/$name.out" "RS_${upper}_ERR=$TMP/$name.err"
done

# ----- Report -----
echo "## Queue status — $NOW"
echo ""
REPORT_RC=0
python3 "$SCRIPT_DIR/report.py" || REPORT_RC=$?
if (( REPORT_RC != 0 )); then
  echo "**Report**: report.py exited $REPORT_RC, so the summary and details above may be cut short"
  echo ""
fi

# ----- Open sidecars -----
#
# "None open" and "could not list" must never read alike. A failed `sidecar
# list` used to be replaced with an empty listing, so this section said
# "(none)" and the script exited 0. Now a CLI missing from PATH, a non-zero
# exit, or output that is not a listing prints "could not list" and why, and
# the script exits 1 once the report is out.
SIDECARS_RC=0
if [[ "$RS_SIDECARS_RC" != "missing" ]]; then
  SC_RC="$RS_SIDECARS_RC" SC_OUT="$RS_SIDECARS_OUT" SC_ERR="$RS_SIDECARS_ERR" python3 - <<'PY' || SIDECARS_RC=$?
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
print(
    "_Requests tasks have filed that still wait for your answer. One request can hold "
    "several questions; one with no questions is a notice to acknowledge. Answer them with "
    "/runner-answer-sidecar. A task keeps the status awaiting_sidecar after you answer, until "
    "it runs again, so that status alone does not mean a question is open._"
)
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
