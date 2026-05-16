#!/bin/bash
# Verify no per-branch model contributions were lost from a structured-
# markdown union file (e.g. covariate-columns.md) after a bulk merge.
#
# For each source branch matching the pattern, extracts every distinct
# `*.R` model filename that branch added to the file (vs the merge base)
# and asserts it appears somewhere in the post-merge file.
#
# Usage:
#   verify_branch_contributions.sh [OPTIONS]
#
# Options:
#   --repo <path>           Target git repo (default: $PWD).
#   --branch <name>         Merge branch with worktree at <repo>/.worktrees/<branch>.
#   --base <ref>            Merge base (default: origin/main).
#   --pattern <glob>        Source branch refspec (default: origin/claude/*).
#   --file <path>           Repo-relative file to verify (default:
#                           inst/references/covariate-columns.md).
#
# Exit codes:
#   0  no contributions missing
#   1  some contributions missing (lists them)
#   2  bad args
set -euo pipefail

REPO="${PWD}"
BRANCH=""
BASE="origin/main"
PATTERN="origin/claude/*"
FILE="inst/references/covariate-columns.md"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --base) BASE="$2"; shift 2 ;;
    --pattern) PATTERN="$2"; shift 2 ;;
    --file) FILE="$2"; shift 2 ;;
    -h|--help) sed -n '2,21p' "$0" | sed 's/^# \?//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$BRANCH" ]]; then
  echo "ERROR: --branch is required" >&2
  exit 2
fi
if [[ -z "$FILE" ]]; then
  # Allow caller to disable the verifier by passing --file "".
  exit 0
fi

WT="$REPO/.worktrees/$BRANCH"
MERGED_FILE="$WT/$FILE"
if [[ ! -f "$MERGED_FILE" ]]; then
  echo "    (verifier) merged file not present at $MERGED_FILE; skipping."
  exit 0
fi

cd "$REPO"

# Iterate the pattern (origin/claude/* etc.).
mapfile -t BRANCHES < <(
  git for-each-ref --format='%(refname:short)' "refs/remotes/$PATTERN" 2>/dev/null | sort -u
)

MISSING_COUNT=0
MISSING_DETAILS=""
for br in "${BRANCHES[@]}"; do
  short=${br#origin/}
  diff_out=$(git diff "$BASE..$br" -- "$FILE" 2>/dev/null || true)
  [[ -z "$diff_out" ]] && continue
  # Extract distinct *.R filenames from + (added) lines only.
  branch_files=$(echo "$diff_out" \
    | grep -E "^\+[^+]" \
    | grep -oE '`[A-Za-z][^`]*\.R`' \
    | sort -u)
  [[ -z "$branch_files" ]] && continue
  branch_missing=""
  while IFS= read -r fname; do
    [[ -z "$fname" ]] && continue
    if ! grep -Fq -- "$fname" "$MERGED_FILE" 2>/dev/null; then
      branch_missing="$branch_missing $fname"
    fi
  done <<< "$branch_files"
  if [[ -n "$branch_missing" ]]; then
    MISSING_COUNT=$((MISSING_COUNT + 1))
    MISSING_DETAILS="$MISSING_DETAILS    $short:$branch_missing\n"
  fi
done

if (( MISSING_COUNT == 0 )); then
  echo "    (verifier) OK — all per-branch *.R additions are present in $FILE"
  exit 0
else
  echo
  echo "ERROR: (verifier) $MISSING_COUNT branch(es) have *.R contributions missing from $FILE:"
  printf "%b" "$MISSING_DETAILS"
  echo
  echo "    Worktree left at: $WT"
  echo "    Either re-run the union-merger, or hand-merge the missing entries."
  exit 1
fi
