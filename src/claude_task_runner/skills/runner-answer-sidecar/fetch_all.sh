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
#     "sidecars": [
#       {
#         "task_id": "...", "sequence": <n>,
#         "summary": "...", "context": "...",
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

out = {"queue": queue, "n_open": len(sidecars), "sidecars": []}

for s in sidecars:
    tid = s["task_id"]
    seq = s["sequence"]
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
                "questions": qs,
                "schema_warning": "v1 schema (legacy); read directly from request file",
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
        })
        continue
    out["sidecars"].append({
        "task_id": tid,
        "sequence": seq,
        "summary": d.get("summary", ""),
        "context": d.get("context", ""),
        "questions": d.get("questions", []),
    })

print(json.dumps(out, indent=2))
EOF
