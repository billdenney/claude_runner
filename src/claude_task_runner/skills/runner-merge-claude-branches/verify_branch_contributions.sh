#!/bin/bash
# Verify no per-branch model contributions were lost from a structured-
# markdown union file (e.g. covariate-columns.md) after a bulk merge.
#
# Two independent checks, both gated on the same per-branch candidate set:
#
#   1. Filename check (inline): every distinct `*.R` model filename a
#      branch added to the file (vs the merge base) must appear somewhere
#      in the post-merge file. Catches lost `**Example models:**` entries
#      whose .R is unique to the lost section.
#
#   2. Section-header check (delegated to verify_section_headers.py):
#      every brand-new `## ` or `### CANONICAL_NAME` header a branch
#      introduces (vs the merge base) must appear in the post-merge file.
#      Catches whole sections clobbered by `-X theirs` when the same .R
#      is referenced elsewhere in the file (the filename check passes
#      but the section is silently lost).
#
# Usage:
#   verify_branch_contributions.sh [OPTIONS]
#
# Options:
#   --repo <path>           Target git repo (default: $PWD).
#   --branch <name>         Merge branch with worktree at <repo>/.worktrees/<branch>.
#   --base <ref>            Merge base (default: origin/main).
#   --pattern <glob>        Source branch refspec (default: origin/claude/*).
#   --extra-ref <refname>   Additional ref to include (repeatable). Use for
#                           hand-picked branches outside the pattern, e.g.
#                           origin/add-Fiedler-Kelly_2019_fremanezumab.
#   --file <path>           Repo-relative file to verify (default:
#                           inst/references/covariate-columns.md).
#
# Exit codes:
#   0  no contributions missing
#   1  some contributions missing (lists them; both check types
#      reported in one block when both fail)
#   2  bad args
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

REPO="${PWD}"
BRANCH=""
BASE="origin/main"
PATTERN="origin/claude/*"
FILE="inst/references/covariate-columns.md"
EXTRA_REFS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --base) BASE="$2"; shift 2 ;;
    --pattern) PATTERN="$2"; shift 2 ;;
    --extra-ref) EXTRA_REFS+=("$2"); shift 2 ;;
    --file) FILE="$2"; shift 2 ;;
    -h|--help) sed -n '2,38p' "$0" | sed 's/^# \?//'; exit 0 ;;
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

# Iterate the pattern (origin/claude/* etc.) plus any --extra-ref additions.
mapfile -t BRANCHES < <(
  git for-each-ref --format='%(refname:short)' "refs/remotes/$PATTERN" 2>/dev/null | sort -u
)
for er in "${EXTRA_REFS[@]:-}"; do
  [[ -z "$er" ]] && continue
  # Avoid duplicates if the operator passed something already in the pattern.
  if [[ ! " ${BRANCHES[*]} " =~ " ${er} " ]]; then
    BRANCHES+=("$er")
  fi
done

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

# Section-header check (delegated to verify_section_headers.py; catches
# brand-new ##/### canonical-section headers a branch introduced that
# `-X theirs` later clobbered, even when the filename check above passed).
SECTION_FAIL=0
SECTION_OUTPUT=""
HEADER_SCRIPT="$SCRIPT_DIR/verify_section_headers.py"
if [[ -f "$HEADER_SCRIPT" ]]; then
  if ! command -v python3 >/dev/null 2>&1; then
    echo "    (verifier) WARN: python3 not found in PATH; skipping ##/### header check."
  else
    section_args=(
      --repo "$REPO"
      --branch "$BRANCH"
      --base "$BASE"
      --pattern "$PATTERN"
      --file "$FILE"
    )
    for er in "${EXTRA_REFS[@]:-}"; do
      [[ -n "$er" ]] && section_args+=( --extra-ref "$er" )
    done
    # Capture output so we can interleave both reports cleanly under one footer.
    SECTION_OUTPUT="$(python3 "$HEADER_SCRIPT" "${section_args[@]}")" || SECTION_FAIL=1
  fi
fi

if (( MISSING_COUNT == 0 && SECTION_FAIL == 0 )); then
  echo "    (verifier) OK — all per-branch *.R additions and brand-new ##/### canonical-section headers are present in $FILE"
  exit 0
fi

if (( MISSING_COUNT > 0 )); then
  echo
  echo "ERROR: (verifier) $MISSING_COUNT branch(es) have *.R contributions missing from $FILE:"
  printf "%b" "$MISSING_DETAILS"
fi
if (( SECTION_FAIL )); then
  printf "%s\n" "$SECTION_OUTPUT"
fi
echo
echo "    Worktree left at: $WT"
echo "    Either re-run the union-merger, or hand-merge the missing entries."
exit 1
