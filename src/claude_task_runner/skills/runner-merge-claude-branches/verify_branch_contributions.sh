#!/bin/bash
# Verify no per-branch model contributions were lost from a structured-
# markdown union file (e.g. covariate-columns.md) after a bulk merge.
#
# Three independent checks, all gated on the same merge set (merge_set.py):
# the refs matching --pattern, plus any --extra-ref, that the consolidation
# branch actually merged, each read at the commit it merged. A ref left out
# with merge_branches.sh --exclude-ref, or pushed after the survey, has no
# contribution to check; one whose tip moved on after the merge is checked for
# the part that was merged. What a branch "added" is its own diff, from its
# fork point on the base to the merged commit.
#
#   1. Filename check (inline): every distinct `*.R` model filename a
#      branch added to the file must appear somewhere in the post-merge
#      file. Catches lost `**Example models:**` entries whose .R is unique
#      to the lost section.
#
#   2. Section-header check (delegated to verify_section_headers.py):
#      every brand-new `## ` or `### CANONICAL_NAME` header a branch
#      introduces must appear in the post-merge file. Catches whole
#      sections clobbered by `-X theirs` when the same .R is referenced
#      elsewhere in the file (the filename check passes but the section is
#      silently lost).
#
#   3. Placement check (delegated to verify_register_placement.py): every
#      (canonical, model.R) pair a folded-in branch recorded must still be
#      filed under that canonical. Catches the entry -X theirs discards when
#      two branches register the same canonical, which passes both checks
#      above.
#
# Usage:
#   verify_branch_contributions.sh [OPTIONS]
#
# Options:
#   --repo <path>           Target git repo (default: $PWD).
#   --branch <name>         Merge branch with worktree at <repo>/.worktrees/<branch>.
#   --base <ref>            Base the branches were merged into (default: origin/main).
#   --pattern <glob>        Source branch refspec (default: origin/claude/*).
#   --extra-ref <refname>   Additional ref to include (repeatable). Use for
#                           hand-picked branches outside the pattern, e.g.
#                           origin/add-Fiedler-Kelly_2019_fremanezumab.
#   --file <path>           Repo-relative file to verify (default:
#                           inst/references/covariate-columns.md). Pass ""
#                           to disable the verifier.
#   -h, --help              Show this help.
#
# Exit codes:
#   0  no contributions missing; also when --file is "" or the file is not
#      in the worktree (nothing to verify)
#   1  some contributions missing (lists them; every failing check is
#      reported in one block)
#   2  the checks could not run: a bad argument, a --base, --branch or
#      --extra-ref that does not resolve, no worktree for --branch, no
#      branch matching --pattern, an empty merge set, python3 missing, or a
#      failed git command
set -euo pipefail
# merge_branches.sh reads exit 1 as a verdict ("contributions missing") and
# carries on, so an unexpected failure must not exit 1 as well.
trap 'echo "ERROR: (verifier) unexpected failure (exit $?) at line $LINENO" >&2; exit 2' ERR

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

REPO="${PWD}"
BRANCH=""
BASE="origin/main"
PATTERN="origin/claude/*"
FILE="inst/references/covariate-columns.md"
EXTRA_REFS=()

die() {
  echo "ERROR: (verifier) $*" >&2
  exit 2
}

need_value() {
  [[ $# -ge 2 ]] || die "$1 needs a value"
}

resolves() {
  git rev-parse --verify --quiet "$1^{commit}" >/dev/null
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) need_value "$@"; REPO="$2"; shift 2 ;;
    --branch) need_value "$@"; BRANCH="$2"; shift 2 ;;
    --base) need_value "$@"; BASE="$2"; shift 2 ;;
    --pattern) need_value "$@"; PATTERN="$2"; shift 2 ;;
    --extra-ref) need_value "$@"; EXTRA_REFS+=("$2"); shift 2 ;;
    --file) need_value "$@"; FILE="$2"; shift 2 ;;
    -h|--help)
      awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
      exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

[[ -n "$BRANCH" ]] || die "--branch is required"
if [[ -z "$FILE" ]]; then
  # Allow caller to disable the verifier by passing --file "".
  exit 0
fi

cd "$REPO" 2>/dev/null || die "--repo $REPO is not a directory"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "--repo $REPO is not a git working tree"
resolves "$BASE" || die "--base '$BASE' does not resolve to a commit in $REPO"
resolves "$BRANCH" || die "--branch '$BRANCH' does not resolve to a commit in $REPO"
WT="$REPO/.worktrees/$BRANCH"
[[ -d "$WT" ]] || die "no worktree for --branch '$BRANCH' at $WT"
# Both delegated checks need python3; without it they would be skipped and a
# lost section reported as OK.
command -v python3 >/dev/null 2>&1 || die "python3 is not on PATH, so the header and placement checks cannot run"
HEADER_SCRIPT="$SCRIPT_DIR/verify_section_headers.py"
PLACEMENT_SCRIPT="$SCRIPT_DIR/verify_register_placement.py"
MERGE_SET_SCRIPT="$SCRIPT_DIR/merge_set.py"
for script in "$HEADER_SCRIPT" "$PLACEMENT_SCRIPT" "$MERGE_SET_SCRIPT"; do
  [[ -f "$script" ]] || die "$script is missing"
done

# The candidates: the pattern (origin/claude/* etc.) plus any --extra-ref.
refs=$(git for-each-ref --format='%(refname:short)' "refs/remotes/$PATTERN") \
  || die "git for-each-ref failed for --pattern '$PATTERN'"
CANDIDATES=0
while IFS= read -r ref; do
  [[ -n "$ref" ]] && CANDIDATES=$((CANDIDATES + 1))
done <<< "$refs"
for er in "${EXTRA_REFS[@]:-}"; do
  [[ -z "$er" ]] && continue
  resolves "$er" || die "--extra-ref '$er' does not resolve to a commit in $REPO"
  CANDIDATES=$((CANDIDATES + 1))
done
# Checking no branch at all would report every contribution present.
(( CANDIDATES > 0 )) \
  || die "no branch matches --pattern '$PATTERN' and no --extra-ref was given; nothing to verify"

MERGED_FILE="$WT/$FILE"
if [[ ! -f "$MERGED_FILE" ]]; then
  echo "    (verifier) merged file not present at $MERGED_FILE; skipping."
  exit 0
fi

delegated_args=(
  --repo "$REPO"
  --branch "$BRANCH"
  --base "$BASE"
  --pattern "$PATTERN"
)
for er in "${EXTRA_REFS[@]:-}"; do
  [[ -n "$er" ]] && delegated_args+=( --extra-ref "$er" )
done

# One "<ref> <merged commit> <fork point>" line per member of the merge set.
# The delegated verifiers report what the gate left out, so this one is quiet.
MEMBERS=$(python3 "$MERGE_SET_SCRIPT" --quiet "${delegated_args[@]}") \
  || die "merge_set.py could not compute the merge set of '$BRANCH'"

MISSING_COUNT=0
MISSING_DETAILS=""
while read -r br merged fork; do
  [[ -z "$br" ]] && continue
  short=${br#origin/}
  diff_out=$(git diff "$fork" "$merged" -- "$FILE") \
    || die "git diff $fork $merged -- $FILE failed for $br"
  [[ -z "$diff_out" ]] && continue
  # Extract distinct *.R filenames from + (added) lines only. grep exits 1
  # when nothing matches, which is an answer here rather than an error.
  branch_files=$(printf '%s\n' "$diff_out" \
    | { grep -E "^\+[^+]" || [[ $? -eq 1 ]]; } \
    | { grep -oE '`[A-Za-z][^`]*\.R`' || [[ $? -eq 1 ]]; } \
    | sort -u)
  [[ -z "$branch_files" ]] && continue
  branch_missing=""
  while IFS= read -r fname; do
    [[ -z "$fname" ]] && continue
    if ! grep -Fq -- "$fname" "$MERGED_FILE"; then
      branch_missing="$branch_missing $fname"
    fi
  done <<< "$branch_files"
  if [[ -n "$branch_missing" ]]; then
    MISSING_COUNT=$((MISSING_COUNT + 1))
    MISSING_DETAILS="$MISSING_DETAILS    $short:$branch_missing\n"
  fi
done <<< "$MEMBERS"

delegated_args+=( --file "$FILE" )

# Section-header check (delegated to verify_section_headers.py; catches
# brand-new ##/### canonical-section headers a branch introduced that
# `-X theirs` later clobbered, even when the filename check above passed).
# Output is captured so both reports interleave cleanly under one footer.
SECTION_RC=0
SECTION_OUTPUT="$(python3 "$HEADER_SCRIPT" "${delegated_args[@]}")" || SECTION_RC=$?

# Placement check (delegated to verify_register_placement.py). The filename
# check above asks whether a branch's *.R appears ANYWHERE in the file; this
# asks whether it is still filed UNDER THE CANONICAL the branch filed it under.
# Both of the weaker checks pass when two branches register the same canonical
# and -X theirs keeps one entry, discarding the other's aliases and example
# models -- four such losses survived every existing check on 2026-08-31.
PLACEMENT_RC=0
PLACEMENT_OUTPUT="$(python3 "$PLACEMENT_SCRIPT" "${delegated_args[@]}")" || PLACEMENT_RC=$?

# Exit 1 means "found missing contributions"; anything higher means the check
# itself could not run, and its report cannot be trusted either way.
(( SECTION_RC <= 1 )) || die "verify_section_headers.py could not run (exit $SECTION_RC)"
(( PLACEMENT_RC <= 1 )) || die "verify_register_placement.py could not run (exit $PLACEMENT_RC)"

if (( MISSING_COUNT == 0 && SECTION_RC == 0 && PLACEMENT_RC == 0 )); then
  [[ -n "$PLACEMENT_OUTPUT" ]] && printf "%s\n" "$PLACEMENT_OUTPUT"
  echo "    (verifier) OK — all per-branch *.R additions and brand-new ##/### canonical-section headers are present in $FILE"
  exit 0
fi

if (( MISSING_COUNT > 0 )); then
  echo
  echo "ERROR: (verifier) $MISSING_COUNT branch(es) have *.R contributions missing from $FILE:"
  printf "%b" "$MISSING_DETAILS"
fi
if (( SECTION_RC )); then
  printf "%s\n" "$SECTION_OUTPUT"
fi
if (( PLACEMENT_RC )); then
  printf "%s\n" "$PLACEMENT_OUTPUT"
fi
echo
echo "    Worktree left at: $WT"
echo "    Either re-run the union-merger, or hand-merge the missing entries."
exit 1
