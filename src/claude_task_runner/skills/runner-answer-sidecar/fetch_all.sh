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
# Exits non-zero if claude-task-runner is missing or list/show errors.
# Tolerates v1-schema (legacy) sidecar requests by capturing whatever
# fields are present and emitting a "schema_warning" field.

set -euo pipefail

QUEUE="${PWD}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --queue) QUEUE="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

LIST_FILE="$(mktemp)"
trap 'rm -f "$LIST_FILE"' EXIT
claude-task-runner sidecar list --queue "$QUEUE" --json > "$LIST_FILE" 2>/dev/null

QUEUE="$QUEUE" LIST_FILE="$LIST_FILE" python3 - <<'EOF'
import json
import os
import subprocess
import sys

queue = os.environ["QUEUE"]
with open(os.environ["LIST_FILE"]) as f:
    listing = json.load(f)
sidecars = listing.get("sidecars", [])

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
