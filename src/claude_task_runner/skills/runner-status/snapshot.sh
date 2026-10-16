#!/bin/bash
# One-pass status snapshot for the Claude task runner.
#
# Usage:
#   ./snapshot.sh [--queue <queue_dir>]
#
# Output: a markdown block. It opens with a plain-language summary
# (report.py summary) of everything below: whether the supervisor is
# running, what each account is doing, how many tasks may run at once and
# why, broken account files, task counts, questions waiting, and one plain
# sentence for each condition that is not normal. The details follow, each
# with a line saying what it shows: supervisor liveness, the supervisor.json
# fields, accounts and concurrency (`account list --json`), the per-account
# usage table from supervisor.json's `accounts` map, queue counts, the
# supervisor's last "no eligible tasks" log line, and the open sidecars.
# Default queue is $PWD. The queue must be an existing directory with a
# todo/ subdirectory.
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
# Everything the summary needs is gathered before anything is printed, so the
# summary can come first. The CLI's JSON is used wherever a command exposes a
# fact: `account list --json` for each account's state, cap, pause marker and
# settings-file error, and for the queue's concurrency ceiling;
# `sidecar list --json` for the open questions.
PIDFILE="$QUEUE/.claude_task_runner/supervisor.pid"
PID=""
PID_STATE="missing"
PID_ETIME=""
if [[ -f "$PIDFILE" ]]; then
  PID="$(cat "$PIDFILE")"
  if [[ -n "$PID" ]] && ps -p "$PID" > /dev/null 2>&1; then
    PID_STATE="alive"
    PID_ETIME="$(ps -p "$PID" -o etime= 2>/dev/null | tr -d ' ' || true)"
  else
    PID_STATE="stale"
  fi
fi

# Run `claude-task-runner <args>` into $TMP/<name>.{out,err,rc}; rc is
# "missing" when the CLI is not on PATH.
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
run_cli accounts account list --queue "$QUEUE" --json
run_cli sidecars sidecar list --queue "$QUEUE" --json

# The live supervisor's newest line about eligible tasks in the journal
# (systemd); report.py also reads <queue>/.claude_task_runner/supervisor.log
# (cron watchdog). Filtering on the PID keeps another queue's supervisor, or
# an earlier run, out of it.
JOURNAL_LINE=""
if [[ "$PID_STATE" == "alive" ]] && command -v journalctl > /dev/null 2>&1; then
  JOURNAL_LINE="$(journalctl --user "_PID=$PID" -r -o cat --no-pager 2> /dev/null \
    | grep -m1 -E 'no eligible tasks:|eligible tasks available again:' || true)"
fi

export RS_QUEUE="$QUEUE" RS_NOW="$NOW" RS_PID_STATE="$PID_STATE" RS_PID_ETIME="$PID_ETIME"
export RS_JOURNAL_LINE="$JOURNAL_LINE"
export RS_ACCOUNTS_RC="$(cat "$TMP/accounts.rc")" RS_ACCOUNTS_OUT="$TMP/accounts.out"
export RS_ACCOUNTS_ERR="$TMP/accounts.err"
export RS_SIDECARS_RC="$(cat "$TMP/sidecars.rc")" RS_SIDECARS_OUT="$TMP/sidecars.out"
export RS_SIDECARS_ERR="$TMP/sidecars.err"

# ----- Report -----
echo "## Queue status — $NOW"
echo ""
python3 "$SCRIPT_DIR/report.py" summary
echo ""
echo "### Details"
echo ""

# ----- Supervisor process liveness -----
if [[ "$PID_STATE" == "alive" ]]; then
  PSLINE="$(ps -p "$PID" -o pid,etime,time,cmd= --no-headers 2>/dev/null || true)"
  echo "**Supervisor**: alive PID $PID"
  echo '```'
  echo "$PSLINE"
  echo '```'
elif [[ "$PID_STATE" == "stale" ]]; then
  echo "**Supervisor**: NOT RUNNING (stale pidfile $PIDFILE → $PID)"
else
  echo "**Supervisor**: NOT RUNNING (no pidfile at $PIDFILE)"
fi
echo ""
echo "_The supervisor is the background process that starts tasks, watches each account's usage and records progress. Nothing new starts while it is not running._"
echo ""

# ----- supervisor.json state -----
SUP_JSON="$QUEUE/.claude_task_runner/supervisor.json"
if [[ -f "$SUP_JSON" ]]; then
  python3 - "$SUP_JSON" <<'PY'
import json, os, sys
from datetime import datetime, timezone
with open(sys.argv[1]) as f:
    d = json.load(f)
saved = datetime.fromtimestamp(os.stat(sys.argv[1]).st_mtime, timezone.utc)
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
    ("last saved", saved.strftime("%Y-%m-%dT%H:%M:%SZ")),
]
print("**supervisor.json**")
print()
print(
    "_The supervisor's own record, rewritten every time it checks the queue (each poll "
    "interval), so `last saved` shows whether it is still working. `state` is its overall "
    "mode; `5h util` and `weekly util` are how much of the 5-hour and weekly usage allowance "
    "is used; `in_flight` counts tasks running now; times are UTC._"
)
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
python3 "$SCRIPT_DIR/report.py" accounts
echo ""

# ----- Per-account state -----
#
# Source the v3 supervisor.json's `accounts` map (one entry per
# configured [[accounts]] block, populated by the multi-account
# /usage capture round-robin from PR 8). Reports each account's
# state, 5h util, weekly util, paused (from the account_paused/
# markers, which supervisor.json's copy trails by up to a tick; from
# supervisor.json on a queue that has no such directory yet), per-account in-flight
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
python3 - "$SUP_JSON" "$QUEUE/.claude_task_runner/account_paused" <<'PY'
import json
import os
import sys
from collections import Counter

with open(sys.argv[1]) as f:
    d = json.load(f)
# Pause markers are the source of truth; None means "use supervisor.json".
pause_dir = sys.argv[2]
pause_note = ""
try:
    markers = {
        n for n in os.listdir(pause_dir)
        if not n.startswith(".") and os.path.isfile(os.path.join(pause_dir, n))
    }
except FileNotFoundError:
    markers = None
except OSError as exc:
    markers = None
    pause_note = f"_paused shows supervisor.json's copy: cannot read {pause_dir}: {exc}_"
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
    "_What each account's last usage reading says. `state`: dispatching or idle take tasks, "
    "slowing_down takes fewer, throttled_5h and throttled_weekly take none until their limit "
    "resets, no_reading takes none until a reading arrives. `in-flight` is how many tasks the "
    "account is running now; `target` (the throttle target) is how many the throttle lets it "
    "run, \"—\" when no reading has set one; `wakeup` is when the supervisor looks again; times "
    "are UTC._"
)
print()
print(
    "| account | state | 5h | weekly | paused | in-flight | target | "
    "5h reset | weekly reset | wakeup | last capture |"
)
print("|---|---|---:|---:|:-:|---:|---:|---|---|---|---|")
for name in sorted(accounts):
    a = accounts[name]
    is_paused = (name in markers) if markers is not None else a.get("paused")
    paused = "yes" if is_paused else ""
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
if pause_note:
    print()
    print(pause_note)
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
echo ""
fi

# ----- Queue counts and the last idle report -----
python3 "$SCRIPT_DIR/report.py" counts
echo ""
python3 "$SCRIPT_DIR/report.py" idle
echo ""

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
    "_Questions tasks have stopped to ask you (a task waiting on one is awaiting_sidecar). "
    "One request can hold several questions; a request with no questions is a notice to "
    "acknowledge. Answer them with /runner-answer-sidecar._"
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
