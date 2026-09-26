#!/bin/bash
# Fetch every open sidecar's full context in one pass.
#
# Usage:
#   ./fetch_all.sh [--queue <queue_dir>]
#
# Output: pretty-printed JSON to stdout with shape:
#   {
#     "queue": "<dir>",
#     "n_open": <int>,
#     "n_outstanding_questions": <int>,
#     "sidecars": [
#       {
#         "task_id": "...", "sequence": <n>,
#         "summary": "...", "context": "...",
#         "outstanding": ["q2", "q3"], "answered": ["q1"], "partial": <bool>,
#         "questions": [
#           {"id": "...", "prompt": "...", "options": [...],
#            "multi_select": <bool>, "allow_free_text": <bool>,
#            "recommended": "..."}
#         ]
#       },
#       ...
#     ]
#   }
#
# `questions` holds ONLY the questions still outstanding. A request whose
# response answered q1 but not q2/q3 is still open, and re-presenting q1
# would waste the operator's clicks -- and `sidecar answer` requires every
# asked id, so the caller must resupply q1's recorded answer alongside the
# new ones (`answered` names them; the response file holds the values).
#
# Exit codes, with nothing on stdout unless 0:
#   0  the JSON above
#   1  the open sidecars could not be listed: claude-task-runner is not on
#      PATH, `sidecar list` failed, or it printed no listing. stderr says
#      why and shows the last lines the command printed.
#   2  bad args, or the queue is missing or not a queue (no todo/)
# A sidecar that `sidecar show` cannot read does not fail the script: it is
# reported in its entry's "schema_warning" field. v1-schema (legacy)
# requests are read from the request file directly, capturing whatever
# fields are present.

set -euo pipefail

QUEUE="${PWD}"
QUEUE_FROM="the working directory (no --queue given)"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --queue) QUEUE="$2"; QUEUE_FROM="--queue"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Refuse anything that is not a queue. A directory that is not one has no
# sidecars to list, so a mistyped --queue, or a run from the wrong directory
# without one, came back as "n_open": 0, which the skill reports as "No open
# sidecars". A queue is a directory with a todo/ subdirectory, the test
# `worktree reclaim` and the runner-status snapshot.sh apply too.
if [[ ! -d "$QUEUE" ]]; then
  echo "$QUEUE_FROM is not an existing directory: $QUEUE" >&2
  exit 2
fi
if [[ ! -d "$QUEUE/todo" ]]; then
  echo "$QUEUE_FROM is not a queue directory, it has no todo/ subdirectory: $QUEUE" >&2
  exit 2
fi
if ! command -v claude-task-runner > /dev/null 2>&1; then
  echo "could not list the open sidecars: claude-task-runner is not on PATH" >&2
  exit 1
fi

LIST_FILE="$(mktemp)"
LIST_ERR="$(mktemp)"
trap 'rm -f "$LIST_FILE" "$LIST_ERR"' EXIT
# A failed listing is reported by the heredoc below. This call used to send
# stderr to /dev/null, and `sidecar list --json` prints its own errors on
# stdout, into LIST_FILE, so set -e ended the script with no message at all.
LIST_RC=0
claude-task-runner sidecar list --queue "$QUEUE" --json > "$LIST_FILE" 2> "$LIST_ERR" || LIST_RC=$?

QUEUE="$QUEUE" LIST_FILE="$LIST_FILE" LIST_ERR="$LIST_ERR" LIST_RC="$LIST_RC" python3 - <<'EOF'
import json
import os
import subprocess
import sys

queue = os.environ["QUEUE"]
rc = int(os.environ["LIST_RC"])
with open(os.environ["LIST_FILE"]) as f:
    stdout = f.read()


def could_not_list(why):
    """Exit 1, with ``why`` and the last lines the listing printed on stderr."""
    with open(os.environ["LIST_ERR"]) as f:
        printed = (stdout.rstrip("\n") + "\n" + f.read()).strip().splitlines()
    sys.exit("\n".join([f"could not list the open sidecars: {why}", *printed[-20:]]))


# A listing that failed, or is not a listing, must never read as "n_open": 0.
if rc != 0:
    could_not_list(f"`claude-task-runner sidecar list --json` exited {rc}")
try:
    listing = json.loads(stdout)
    sidecars = listing["sidecars"]
except (ValueError, KeyError, TypeError) as exc:
    could_not_list(f"`claude-task-runner sidecar list --json` printed no listing ({exc!r})")

out = {
    "queue": queue,
    "n_open": listing.get("n_open", len(sidecars)),
    "n_outstanding_questions": listing.get("n_outstanding_questions"),
    "sidecars": [],
}


def carry(s):
    """Per-question fields `sidecar list` already computed."""
    return {
        "outstanding": s.get("outstanding", []),
        "answered": s.get("answered", []),
        "partial": s.get("partial", False),
        "response_path": s.get("response_path"),
    }


for s in sidecars:
    tid = s["task_id"]
    seq = s["sequence"]
    outstanding = set(s.get("outstanding") or [])
    if s.get("error"):
        # Request unreadable: openness could not be decided from its
        # question ids, so it is reported open on purpose. Surface it.
        out["sidecars"].append({
            "task_id": tid, "sequence": seq,
            "schema_warning": s["error"],
            **carry(s),
        })
        continue
    try:
        raw = subprocess.run(
            ["claude-task-runner", "sidecar", "show", tid, str(seq),
             "--queue", queue, "--json"],
            capture_output=True, text=True, timeout=15,
        )
    except subprocess.TimeoutExpired:
        out["sidecars"].append({
            "task_id": tid, "sequence": seq,
            "schema_warning": "show command timed out",
            **carry(s),
        })
        continue
    if raw.returncode != 0:
        # v1-schema legacy requests fail validation; read raw file
        path = f"{queue}/.claude_task_runner/sidecar/{tid}/request-{seq:03d}.json"
        try:
            with open(path) as f:
                d = json.load(f)
            qs = d.get("questions") or [
                {
                    "id": "q1",
                    "prompt": d.get("question", ""),
                    "options": [
                        {"value": o.get("id", o.get("value", "")),
                         "label": o.get("label", ""),
                         "description": o.get("description", "")}
                        for o in (d.get("options") or [])
                    ],
                    "multi_select": False,
                    "allow_free_text": True,
                    "recommended": next(
                        (o.get("id") for o in (d.get("options") or []) if o.get("recommended")),
                        None
                    ),
                }
            ]
            out["sidecars"].append({
                "task_id": tid, "sequence": seq,
                "summary": d.get("summary", ""),
                "context": d.get("context", d.get("details", "")),
                "questions": [q for q in qs if q.get("id") in outstanding],
                "schema_warning": "v1 schema (legacy); read directly from request file",
                **carry(s),
            })
        except Exception as e:
            out["sidecars"].append({
                "task_id": tid, "sequence": seq,
                "schema_warning": f"failed to read sidecar: {e}",
            })
        continue
    try:
        d = json.loads(raw.stdout)
    except Exception as e:
        out["sidecars"].append({
            "task_id": tid, "sequence": seq,
            "schema_warning": f"parse error: {e}",
            **carry(s),
        })
        continue
    out["sidecars"].append({
        "task_id": tid,
        "sequence": seq,
        "summary": d.get("summary", ""),
        "context": d.get("context", ""),
        # Outstanding only -- see the header note.
        "questions": [q for q in d.get("questions", []) if q.get("id") in outstanding],
        **carry(s),
    })

print(json.dumps(out, indent=2))
EOF
