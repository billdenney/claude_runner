#!/bin/bash
# Consolidate per-task claude/* branches into one review-ready branch.
#
# End-to-end orchestrator for /runner-merge-claude-branches. Runs:
#   1. Pre-flight survey (which branches have unmerged commits; stops if two
#      branches add one path with different content)
#   2. Worktree creation off the configured base
#   3. Sequential merge with -X theirs
#   4. Resurrected-path guard (re-remove files the base deleted that a
#      stale branch brought back via -X theirs)
#   5. Register repairs: union-merge of covariate-columns.md (recovers the
#      annotations -X theirs would have lost), dedup of duplicate canonical
#      headers, restore of dropped canonical blocks, then the contribution
#      verifier. Every repair and verify step reads only the merge set: the
#      branches this run merged, each at the commit it merged (merge_set.py).
#   6. Union-merge of NEWS.md
#   7. R-side registry regeneration (buildModelDb + document)
#   8. devtools::check pre-push gate
#   9. Parallel vignette validation pre-push gate
#  10. Push branch + print PR title/body
#
# Usage:
#   merge_branches.sh [OPTIONS]
#
# Options:
#   --repo <path>           Target git repo (default: $PWD).
#   --base <ref>            Base branch to merge into (default: origin/main).
#   --pattern <glob>        Refspec pattern for source branches
#                           (default: origin/claude/*).
#   --extra-ref <refname>   Additional fully-qualified ref to include
#                           (repeatable). Use for hand-picked feature
#                           branches that don't match --pattern, e.g.
#                           --extra-ref origin/add-Fiedler-Kelly_2019_fremanezumab.
#                           Flows through to every repair and verify step.
#   --exclude-ref <refname> Fully-qualified ref to leave out even though the
#                           pattern matches it (repeatable), e.g. a WIP task
#                           branch: --exclude-ref origin/claude/oasweep_PMC6813168.
#   --branch-name <name>    New consolidation branch name
#                           (default: merge-all-claude-branches-<YYYY-MM-DD>).
#   --forbid-path <path>    Repo-relative path that must NOT come back if the
#                           BASE has deleted it. After merging, if the path is
#                           absent on --base but present in the merge result, a
#                           stale branch resurrected it via -X theirs; the guard
#                           re-removes it and commits. No-op while the base
#                           still has the path. Repeatable. Default:
#                           inst/modeldb.qs2. Pass "" to disable.
#   --union-file <path>     Structured markdown file requiring union merge
#                           (default: inst/references/covariate-columns.md).
#                           Pass "" to disable the union step.
#   --register-file <path>  Another '### CANONICAL' register that -X theirs
#                           can gut; it gets the restore, a per-## section
#                           dedup and the verifier (repeatable). Defaults:
#                           inst/references/compartment-names.md and
#                           inst/references/parameter-names.md.
#   --no-register-files     Clear the register list, defaults included; a
#                           later --register-file adds to the emptied list.
#   --skip-r-regen          Skip step 7 (buildModelDb / document). Use when
#                           merging into a repo that doesn't have these.
#   --skip-check            Skip step 8 (devtools::check). Use for fast
#                           iteration; the operator runs check separately.
#   --skip-vignettes        Skip step 9 (parallel vignette validation).
#                           Use only when iterating; not recommended for
#                           the final pre-push run because pkgdown CI's
#                           sequential vignette build will surface the
#                           failures one at a time.
#   --vignette-jobs <N>     Parallel workers for vignette validation
#                           (default: max(1, ncpus - 2)).
#   --vignette-timeout <S>  Per-vignette wall-clock ceiling in seconds
#                           (default: 900). Increase if you have a model
#                           that legitimately needs >15 minutes.
#   --skip-push             Don't push the branch (steps 1-9 only).
#   --dry-run               Print the survey and exit before creating the worktree.
#   --yes                   Don't prompt; assume yes to "create worktree".
#                           Required when stdin is not a terminal, as in an
#                           agent's shell: without it the script stops at the
#                           prompt with exit 3 instead of merging.
#   -h, --help              Show this help.
#
# Exit codes:
#   0  success; also a completed --dry-run, no unmerged branches, or the
#      operator answering no at the prompt
#   1  an unexpected command failure
#   2  bad arguments: an unknown flag, a flag missing its value, or a
#      non-numeric --vignette-jobs / --vignette-timeout
#   3  pre-flight failure: --repo is not a git working tree, python3 missing,
#      Rscript missing while an R step is enabled, git fetch failed, --base or
#      an --extra-ref does not resolve, no branch matched, a survey diff
#      failed (no merge base, or a shallow clone), no --yes without a
#      terminal, or the worktree already exists
#   4  nothing merged: two branches add one path with different content (the
#      survey stops before merging), or every merge failed
#   5  a repair or verification step failed: a union-merge, dedup or restore
#      script failed, duplicate canonical headers survived dedup, the
#      verifier could not run, or the R registry regeneration failed
#   6  devtools::check failed
#   7  push failed
#   8  parallel vignette validation failed, or the validator could not run
set -euo pipefail
trap 'echo "ERROR: unexpected failure (exit $?) at line $LINENO" >&2; exit 1' ERR

# Resolved once: the union-merger, the dedup, the dropped-section restore and
# the NEWS union all need it, and they no longer all live inside the same
# conditional block (see NOTE ON ORDER below).
PYTHON3="$(command -v python3 || true)"

REPO="${PWD}"
BASE="origin/main"
PATTERN="origin/claude/*"
BRANCH_NAME=""
UNION_FILE="inst/references/covariate-columns.md"
# Register files that are NOT the Example-models union file but are still
# structured '### CANONICAL' registers that -X theirs can silently gut.  The
# 2026-08-31 consolidation had 23 branches touching compartment-names.md and 11
# touching parameter-names.md while every repair ran only against UNION_FILE;
# 11 canonical blocks were lost.  These get restore + dedup + placement checks.
REGISTER_FILES=("inst/references/compartment-names.md" "inst/references/parameter-names.md")
FORBID_PATHS=("inst/modeldb.qs2")
SKIP_R_REGEN=0
SKIP_CHECK=0
SKIP_VIGNETTES=0
VIGNETTE_JOBS=""
VIGNETTE_TIMEOUT=900
SKIP_PUSH=0
DRY_RUN=0
ASSUME_YES=0
EXTRA_REFS=()
EXCLUDE_REFS=()

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  # The whole header comment, however long it grows.
  awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"
}

die() {  # <exit code> <message>
  local code=$1
  shift
  echo "ERROR: $*" >&2
  exit "$code"
}

need_value() {
  [[ $# -ge 2 ]] || die 2 "$1 needs a value"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) need_value "$@"; REPO="$2"; shift 2 ;;
    --base) need_value "$@"; BASE="$2"; shift 2 ;;
    --pattern) need_value "$@"; PATTERN="$2"; shift 2 ;;
    --extra-ref) need_value "$@"; EXTRA_REFS+=("$2"); shift 2 ;;
    --exclude-ref) need_value "$@"; EXCLUDE_REFS+=("$2"); shift 2 ;;
    --branch-name) need_value "$@"; BRANCH_NAME="$2"; shift 2 ;;
    --union-file) need_value "$@"; UNION_FILE="$2"; shift 2 ;;
    --register-file) need_value "$@"; REGISTER_FILES+=("$2"); shift 2 ;;
    --no-register-files) REGISTER_FILES=(); shift ;;
    --forbid-path)
      need_value "$@"
      if [[ -z "$2" ]]; then FORBID_PATHS=(); else
        if [[ "${FORBID_PATHS[*]}" == "inst/modeldb.qs2" ]]; then FORBID_PATHS=(); fi
        FORBID_PATHS+=("$2")
      fi; shift 2 ;;
    --skip-r-regen) SKIP_R_REGEN=1; shift ;;
    --skip-check) SKIP_CHECK=1; shift ;;
    --skip-vignettes) SKIP_VIGNETTES=1; shift ;;
    --vignette-jobs) need_value "$@"; VIGNETTE_JOBS="$2"; shift 2 ;;
    --vignette-timeout) need_value "$@"; VIGNETTE_TIMEOUT="$2"; shift 2 ;;
    --skip-push) SKIP_PUSH=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --yes) ASSUME_YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown arg: $1" >&2; usage; exit 2 ;;
  esac
done

# Default vignette parallelism: ncpus - 2 (with a floor of 1).
if [[ -z "$VIGNETTE_JOBS" ]]; then
  if command -v nproc >/dev/null 2>&1; then
    VIGNETTE_JOBS=$(( $(nproc) - 2 ))
  else
    VIGNETTE_JOBS=4
  fi
  [[ "$VIGNETTE_JOBS" -lt 1 ]] && VIGNETTE_JOBS=1
fi
[[ "$VIGNETTE_JOBS" =~ ^[1-9][0-9]*$ ]] || die 2 "--vignette-jobs must be a positive integer, not '$VIGNETTE_JOBS'"
[[ "$VIGNETTE_TIMEOUT" =~ ^[1-9][0-9]*$ ]] || die 2 "--vignette-timeout must be a positive integer, not '$VIGNETTE_TIMEOUT'"

if [[ -z "$BRANCH_NAME" ]]; then
  BRANCH_NAME="merge-all-claude-branches-$(date -u +%F)"
fi

cd "$REPO" 2>/dev/null || die 3 "--repo $REPO is not a directory"
# Absolute from here on: the helpers get --repo "$REPO" after the script has
# moved into the worktree, where a relative path would point somewhere else.
REPO="$(pwd)"

# Repo sanity.
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die 3 "$REPO is not a git working tree."

# Fetch so origin refs are current.
echo "==> Fetching $BASE / source refs (--prune)"
git fetch origin --prune 2>&1 | tail -5 || die 3 "git fetch origin --prune failed"

# A --base that does not resolve would make every branch "0 ahead" and end the
# run with "nothing to do".
git rev-parse --verify --quiet "$BASE^{commit}" >/dev/null || die 3 "--base '$BASE' does not resolve to a commit"

# Survey.
echo
echo "==> Surveying $PATTERN branches with unmerged commits vs $BASE"
echo "    base = $(git rev-parse --short "$BASE")"

# Expand pattern to a concrete list of branches under refs/remotes/,
# then append any --extra-ref entries.
matches=$(git for-each-ref --format='%(refname:short)' "refs/remotes/$PATTERN") \
  || die 3 "git for-each-ref failed for --pattern '$PATTERN'"
ALL_MATCHES=()
while IFS= read -r m; do
  if [[ -n "$m" ]]; then ALL_MATCHES+=("$m"); fi
done < <(printf '%s\n' "$matches" | sort -u)
# Drop any --exclude-ref entries (a WIP task branch, a branch with its own PR).
# Each exclusion is printed so the survey shows what was left out on purpose;
# an exclusion that matched nothing is a warning, not an error, because the
# branch may simply have been merged and pruned since the command was written.
for ex in "${EXCLUDE_REFS[@]:-}"; do
  [[ -z "$ex" ]] && continue
  kept=(); dropped=0
  for m in "${ALL_MATCHES[@]:-}"; do
    [[ -z "$m" ]] && continue
    if [[ "$m" == "$ex" ]]; then dropped=1; else kept+=("$m"); fi
  done
  if (( dropped )); then
    echo "    excluding $ex (--exclude-ref)"
    # Not ("${kept[@]:-}"): with everything excluded that leaves one empty
    # entry, which the survey would count as the branch "".
    ALL_MATCHES=()
    if (( ${#kept[@]} )); then ALL_MATCHES=("${kept[@]}"); fi
  else
    echo "WARNING: --exclude-ref '$ex' matched no candidate branch" >&2
  fi
done
if [[ ${#ALL_MATCHES[@]} -eq 0 && ${#EXTRA_REFS[@]} -eq 0 ]]; then
  die 3 "no branches matched refspec '$PATTERN' under refs/remotes/ (after any --exclude-ref) and no --extra-ref supplied"
fi
for er in "${EXTRA_REFS[@]:-}"; do
  [[ -z "$er" ]] && continue
  git rev-parse --verify --quiet "$er^{commit}" >/dev/null || die 3 "--extra-ref '$er' does not exist"
  # Avoid duplicates if it's already in the pattern matches.
  in_pattern=0
  for m in "${ALL_MATCHES[@]:-}"; do
    if [[ "$m" == "$er" ]]; then in_pattern=1; break; fi
  done
  if (( ! in_pattern )); then
    ALL_MATCHES+=("$er")
  fi
done
# The same hand-picked refs for every repair and verify step: a step that
# skips them repairs nothing those branches lost.
EXTRA_REF_ARGS=()
for er in "${EXTRA_REFS[@]:-}"; do
  if [[ -n "$er" ]]; then EXTRA_REF_ARGS+=(--extra-ref "$er"); fi
done

UNMERGED=()
for br in "${ALL_MATCHES[@]}"; do
  ahead=$(git rev-list --count "$BASE..$br") || die 3 "cannot count the commits $br has beyond $BASE"
  if [[ "$ahead" != "0" ]]; then
    UNMERGED+=("$br")
  fi
done

if [[ ${#UNMERGED[@]} -eq 0 ]]; then
  echo "==> No unmerged branches found. Nothing to do."
  exit 0
fi

# What a branch changed since it forked from the base (three dots), not the
# difference between the two tips: a branch cut from an older main would
# otherwise count main's later changes as its own files.
# Called as $(branch_diff ...) || exit $?, so die's exit code survives the
# subshell instead of tripping the ERR trap.
branch_diff() {  # <branch> <diff filter, or ""> [path ...]
  local br=$1 filter=$2
  shift 2
  git diff --name-only ${filter:+"--diff-filter=$filter"} "$BASE...$br" -- "$@" \
    || die 3 "cannot diff $br against its merge base with $BASE (no merge base, or a shallow clone?)"
}

echo "    found ${#UNMERGED[@]} unmerged branch(es):"
for br in "${UNMERGED[@]}"; do
  ahead=$(git rev-list --count "$BASE..$br")
  changed=$(branch_diff "$br" "") || exit $?
  files=$(printf '%s\n' "$changed" | grep -c . || true)
  subj=$(git log -1 --format='%s' "$br")
  printf "      %-65s  ahead=%s  files=%s  %s\n" "${br#origin/}" "$ahead" "$files" "${subj:0:60}"
done

# Same-path collision check. Two branches that each ADD a file at the same
# path with different content cannot both survive `-X theirs`: the later merge
# silently replaces the earlier one and a whole model disappears (2026-09-24:
# Wang_2019_tacrolimus.R was added for two different papers). Abort so the
# operator can reletter one branch's files (<Author>_<Year>a_ / <Year>b_, the
# Hansson 2013a/b precedent) or pass --exclude-ref for one of them. Runs before
# the dry-run exit so a dry run reports collisions too.
declare -A ADDED_BY
for br in "${UNMERGED[@]}"; do
  # A diff that fails must stop the survey: skipping the branch would let its
  # collision through unchecked.
  added=$(branch_diff "$br" A inst/modeldb vignettes/articles) || exit $?
  while IFS= read -r p; do
    [[ -z "$p" ]] && continue
    ADDED_BY["$p"]+="$br "
  done <<< "$added"
done
collisions=0
for p in "${!ADDED_BY[@]}"; do
  read -r -a brs <<< "${ADDED_BY[$p]}"
  (( ${#brs[@]} < 2 )) && continue
  nblobs=$(for b in "${brs[@]}"; do git rev-parse "$b:$p"; done | sort -u | wc -l)
  if (( nblobs > 1 )); then
    (( collisions == 0 )) && echo "ERROR: the same new path is added with different content by more than one branch:" >&2
    echo "    $p  <-  ${brs[*]}" >&2
    collisions=$((collisions + 1))
  fi
done
if (( collisions > 0 )); then
  echo "    -X theirs would keep only the last one merged. Reletter one branch's files or --exclude-ref one of them, then re-run." >&2
  exit 4
fi

if (( DRY_RUN )); then
  echo
  echo "==> Dry-run; stopping before worktree creation."
  exit 0
fi

# The repair steps are all python3 scripts, and the R steps need Rscript.
# Checked before anything is created, rather than after the merges.
[[ -n "$PYTHON3" ]] || die 3 "python3 is not on PATH; the union-merge, dedup, restore and verify steps need it."
if (( ! SKIP_R_REGEN || ! SKIP_CHECK || ! SKIP_VIGNETTES )) && ! command -v Rscript >/dev/null 2>&1; then
  die 3 "Rscript is not on PATH. Install R, or pass --skip-r-regen, --skip-check and --skip-vignettes and run those steps elsewhere."
fi

# Confirm.
if (( ! ASSUME_YES )); then
  # Without a terminal, read gets EOF: the run used to end right here with
  # exit 1 and no message, since bash prints no prompt when stdin is not a tty.
  [[ -t 0 ]] || die 3 "stdin is not a terminal, so nobody can confirm merging ${#UNMERGED[@]} branches. Re-run with --yes to proceed without the prompt."
  echo
  read -r -p "Proceed creating worktree and merging ${#UNMERGED[@]} branches? [y/N] " ans || ans=""
  case "$ans" in
    y|Y|yes|YES) ;;
    *) echo "Aborted by operator."; exit 0 ;;
  esac
fi

# Create worktree.
WT_REL=".worktrees/${BRANCH_NAME}"
WT_ABS="$REPO/$WT_REL"
if [[ -d "$WT_ABS" ]]; then
  echo "ERROR: worktree already exists at $WT_ABS" >&2
  echo "  Remove via:" >&2
  echo "    git -C $REPO worktree remove --force $WT_REL" >&2
  echo "    git -C $REPO branch -D $BRANCH_NAME" >&2
  exit 3
fi

mkdir -p .worktrees
echo
echo "==> Creating worktree $WT_REL on new branch $BRANCH_NAME off $BASE"
git worktree add -b "$BRANCH_NAME" "$WT_REL" "$BASE"

# Sequential merge.
#
# We use a real `git merge --no-ff -X theirs` (one merge commit per
# source branch) rather than cherry-pick. The decisive advantage: a
# real merge makes each source branch's tip a true ANCESTOR of the
# consolidation branch. Once the consolidation PR lands on main, every
# folded branch is therefore reported as merged by
# `git branch --merged origin/main` and shown as "Merged" on GitHub —
# with NO SHA rewrite, NO post-merge force-advance dance, and NO
# content-equivalence guesswork to decide whether a branch is already
# in. "Is this branch merged?" becomes a trivial ancestor query.
#
# The historical objection to merge — that merging a branch based on
# an OUTDATED main "rolls back" main-side updates — only ever bites the
# shared bookkeeping files, and every one of those is rebuilt or
# repaired downstream:
#   * binary registry blobs (data/modeldb.rda, inst/modeldb.qs2),
#     man/*.Rd, and the _pkgdown.yml navbar  -> regenerated in step 7;
#   * covariate-columns.md structured lines                -> union-merged in step 5.
# New model .R / vignette .Rmd files live at unique paths, so a 3-way
# merge keeps every prior branch's additions untouched. `-X theirs`
# only changes how CONFLICTING hunks resolve (incoming side wins),
# which is incidental for the regenerated/union-merged files above.
#
# A branch fails here only on a true conflict -X theirs cannot resolve
# (modify/delete, rename/rename); those are aborted and logged, and
# the remaining branches continue.
cd "$WT_ABS"
echo
echo "==> Sequential merge --no-ff -X theirs (one merge commit per branch;"
echo "    binaries regenerated and covariate-columns.md union-merged after)"
SUCCESS=0
FAIL=0
FAILED_LIST=()
MERGED_LIST=()
for br in "${UNMERGED[@]}"; do
  short=${br#origin/}
  ahead=$(git rev-list --count "$BASE..$br")
  echo "    --- $short ($ahead commit(s) ahead) ---"
  if git merge --no-ff --no-edit -X theirs \
        -m "Merge branch '$short' into $BRANCH_NAME" \
        "$br" >/dev/null 2>&1; then
    SUCCESS=$((SUCCESS+1))
    MERGED_LIST+=("$short")
  else
    # modify/delete on a base-deleted path is EXPECTED, not a real conflict:
    # the base dropped the file on purpose and this branch predates that, so it
    # still regenerates it (e.g. every model branch rewrites inst/modeldb.qs2
    # via buildModelDb). git cannot resolve modify/delete with -X theirs, so
    # without this the merge aborts and a perfectly good branch is skipped.
    # Resolve it the only correct way -- keep the deletion -- and finish the
    # merge. Anything else still aborts and is logged.
    mapfile -t UNRES < <(git diff --name-only --diff-filter=U)
    autoresolve=0
    if (( ${#FORBID_PATHS[@]} > 0 )) && (( ${#UNRES[@]} > 0 )); then
      autoresolve=1
      for u in "${UNRES[@]}"; do
        hit=0
        for fp in "${FORBID_PATHS[@]}"; do [[ "$u" == "$fp" ]] && { hit=1; break; }; done
        (( hit )) || { autoresolve=0; break; }
        # only auto-resolve if the BASE really deleted it
        if git cat-file -e "${BASE}:${u}" 2>/dev/null; then autoresolve=0; break; fi
      done
    fi
    if (( autoresolve )); then
      for u in "${UNRES[@]}"; do
        git rm -q -f --ignore-unmatch "$u" >/dev/null 2>&1 || rm -f "$u"
      done
      if git commit -q --no-edit >/dev/null 2>&1; then
        SUCCESS=$((SUCCESS+1))
        MERGED_LIST+=("$short")
        echo "      modify/delete on base-deleted path(s) -> kept deleted: ${UNRES[*]}"
        continue
      fi
    fi
    FAIL=$((FAIL+1))
    FAILED_LIST+=("$short")
    echo "      FAIL — conflicted files -X theirs could not resolve:"
    git diff --name-only --diff-filter=U | sed 's/^/        /'
    echo "      ABORTING this branch; continuing with the rest."
    git merge --abort 2>/dev/null || true
  fi
done

echo
echo "==> Merge summary"
echo "    succeeded: $SUCCESS"
echo "    failed:    $FAIL"
if (( FAIL > 0 )); then
  printf '    failed branches:\n'
  for f in "${FAILED_LIST[@]}"; do echo "      - $f"; done
fi

if (( SUCCESS == 0 )); then
  echo "ERROR: no merges succeeded; nothing to push." >&2
  exit 4
fi

# ---------------------------------------------------------------------------
# Resurrected-path guard.
#
# `-X theirs` happily restores a file the BASE deliberately deleted, if any
# stale source branch still carries it. The branch predates the deletion, so
# there is no conflict for git to report -- the file simply reappears, and the
# consolidation silently undoes a structural change.
#
# The case this was written for: nlmixr2lib dropped `inst/modeldb.qs2` and now
# ships `inst/modeldb.rds`, so readModelDb() no longer needs `qs2` (which was
# only in Suggests yet used unconditionally). Every claude/* branch cut before
# that lands still contains modeldb.qs2, so the next consolidation would
# resurrect it and quietly reintroduce the bug.
#
# The check is conditional on the BASE, so it is correct both before and after
# such a removal lands: if the base still HAS the path, nothing is forbidden
# yet and this is a no-op.
# ---------------------------------------------------------------------------
if (( ${#FORBID_PATHS[@]} > 0 )); then
  echo
  echo "==> Checking for paths resurrected by the merge"
  resurrected=()
  for p in "${FORBID_PATHS[@]}"; do
    if git cat-file -e "${BASE}:${p}" 2>/dev/null; then
      echo "    '$p' still exists on ${BASE} -- not forbidden yet, skipping."
      continue
    fi
    if [[ -e "$p" ]] || git ls-files --error-unmatch "$p" >/dev/null 2>&1; then
      echo "    RESURRECTED: '$p' is absent on ${BASE} but present after merging."
      git rm -q -f --ignore-unmatch "$p" 2>/dev/null || rm -f "$p"
      resurrected+=("$p")
    else
      echo "    ok: '$p' stayed deleted."
    fi
  done
  if (( ${#resurrected[@]} > 0 )); then
    git commit -q -m "Re-remove path(s) resurrected by the merge

-X theirs restored the following from source branches that predate the
deletion on ${BASE}: ${resurrected[*]}

The base deleted these deliberately; a stale branch still carrying the old
file must not undo that. Removed again."
    echo "    committed re-removal of ${#resurrected[@]} path(s)"
  fi
fi

# NOTE ON ORDER (changed 2026-08-20): the register repairs run BEFORE the R
# regeneration, not after. buildModelDb() calls checkModelConventions(), which
# treats a duplicate register entry as an ERROR -- and duplicate entries are
# exactly what `-X theirs` produces when two branches each add the same new
# canonical. Running the regen first therefore aborted the whole pipeline on
# damage that the very next step exists to repair (observed 2026-08-20:
# DIS_PH1, ORGVOL_KIDNEY, FORM_ODT, T_FIRSTDOSE). Repair, then regenerate.
# Union-merge covariate-columns.md (or the configured union-file).
if [[ -n "$UNION_FILE" ]]; then
  echo
  echo "==> Union-merging $UNION_FILE"
  if [[ ! -f "$UNION_FILE" ]]; then
    echo "    union-file not present on this branch; skipping."
  else
    union_args=(
      --repo "$REPO"
      --branch "$BRANCH_NAME"
      --base "$BASE"
      --pattern "$PATTERN"
      --file "$UNION_FILE"
    )
    union_args+=("${EXTRA_REF_ARGS[@]}")
    "$PYTHON3" "$SCRIPT_DIR/union_merge_lines.py" "${union_args[@]}" \
      || die 5 "union_merge_lines.py failed on $UNION_FILE"
    if git diff --quiet -- "$UNION_FILE"; then
      echo "    no diff after union-merge (nothing was lost from -X theirs)."
    else
      git add "$UNION_FILE"
      git commit -m "Reconstruct $UNION_FILE: union of all branches' contributions

The -X theirs strategy used for the bulk merge clobbers structured-
markdown lines that multiple branches independently rewrote. This
commit reconstructs the file by parsing each branch's diff,
unioning per-key annotations, and emitting deduplicated lists.

See /home/bill/.claude/skills/runner-merge-claude-branches for the
union-merger script and the procedural rationale." >/dev/null
      echo "    committed union-merge reconstruction"
    fi
  fi
fi

# Collapse duplicate ### canonical headers the union-merger cannot fix.
# When two branches each ADD the same brand-new ### CANONICAL block
# (both branched from an older main), -X theirs leaves the canonical
# registered twice. union_merge_lines.py folds Example-model lines but
# does not collapse whole duplicate blocks, and verify_section_headers.py
# checks headers survived, not that they are unique. This step collapses
# them (whole-file uniqueness for the covariate register) and then gates:
# a duplicate that somehow survives aborts the run rather than shipping.
if [[ -n "$UNION_FILE" && -f "$UNION_FILE" ]]; then
  echo
  echo "==> Deduping duplicate canonical headers in $UNION_FILE"
  "$PYTHON3" "$SCRIPT_DIR/dedup_canonical_headers.py" --global "$UNION_FILE" \
    || die 5 "dedup_canonical_headers.py failed on $UNION_FILE"
  if ! git diff --quiet -- "$UNION_FILE"; then
    git add "$UNION_FILE"
    git commit -m "Dedup duplicate canonical headers in $UNION_FILE

Two branches that each added the same brand-new ### CANONICAL block
leave exact-duplicate H3 headings after the -X theirs bulk merge.
Collapse each to a single entry, unioning example .R filenames." >/dev/null
    echo "    committed canonical-header dedup"
  else
    echo "    no duplicate canonical headers."
  fi
  if ! "$PYTHON3" "$SCRIPT_DIR/dedup_canonical_headers.py" --global --check "$UNION_FILE"; then
    die 5 "duplicate canonical headers remain in $UNION_FILE after dedup."
  fi
fi

# Dedup the OTHER register files too. The step above only sweeps $UNION_FILE,
# so duplicates -X theirs creates in compartment-names.md / parameter-names.md
# survive -- and buildModelDb() calls checkModelConventions(), which treats a
# duplicate register entry as an ERROR, so they abort the R regen further down.
# Observed 2026-08-22: ndmima and mprotein in compartment-names.md killed the
# round-2 regen after the union-file had already been swept clean.
#
# PER-SECTION scope here, deliberately NOT --global: in compartment-names.md
# the same token is legitimately both a compartment and a metabolite suffix
# (8 such pairs on main), so whole-file uniqueness would delete real entries.
for reg in "${REGISTER_FILES[@]:-}"; do
  [[ -n "$reg" && -f "$reg" ]] || continue
  [[ "$reg" == "$UNION_FILE" ]] && continue
  echo
  echo "==> Deduping duplicate canonical headers in $reg (per-section)"
  "$PYTHON3" "$SCRIPT_DIR/dedup_canonical_headers.py" "$reg" \
    || die 5 "dedup_canonical_headers.py failed on $reg"
  if ! git diff --quiet -- "$reg"; then
    git add "$reg"
    git commit -m "Dedup duplicate canonical headers in $reg" >/dev/null
    echo "    committed dedup of $reg"
  fi
  if ! "$PYTHON3" "$SCRIPT_DIR/dedup_canonical_headers.py" --check "$reg"; then
    die 5 "duplicate canonicals remain in $reg after dedup."
  fi
done

# Restore whole `### CANONICAL` blocks that -X theirs dropped. The union-merger
# folds Example-model LINES inside buckets that already exist; it cannot bring
# back a canonical whose entire block is gone, which is what happens when a
# branch adds a brand-new canonical and a later branch (based on an older main,
# so lacking it) touches the same region. verify_branch_contributions.sh below
# REPORTS that loss but does not repair it, so this was a manual step on every
# large merge -- 21 blocks on 2026-08-20 alone.
RESTORE_TARGETS=()
[[ -n "$UNION_FILE" && -f "$UNION_FILE" ]] && RESTORE_TARGETS+=("$UNION_FILE")
for rf in "${REGISTER_FILES[@]:-}"; do
  [[ -n "$rf" && -f "$rf" ]] && RESTORE_TARGETS+=("$rf")
done
if (( ${#RESTORE_TARGETS[@]} )); then
  echo
  echo "==> Restoring canonical blocks dropped by the merge"
  for rf in "${RESTORE_TARGETS[@]}"; do
    restore_args=(
      --repo "$REPO"
      --branch "$BRANCH_NAME"
      --base "$BASE"
      --pattern "$PATTERN"
      --file "$rf"
      "${EXTRA_REF_ARGS[@]}"
    )
    echo "    -- $rf"
    "$PYTHON3" "$SCRIPT_DIR/restore_dropped_sections.py" "${restore_args[@]}" \
      || die 5 "restore_dropped_sections.py failed on $rf"
    if ! git diff --quiet -- "$rf"; then
      git add "$rf"
      git commit -m "Restore canonical blocks dropped by -X theirs in $rf" >/dev/null
      echo "       committed restored blocks"
    fi
  done
  # The extra registers are per-##-section scoped for dedup (the same token can
  # legitimately be both a compartment and a suffix), unlike the covariate
  # register which is globally unique and deduped with --global above.
  for rf in "${REGISTER_FILES[@]:-}"; do
    [[ -n "$rf" && -f "$rf" ]] || continue
    "$PYTHON3" "$SCRIPT_DIR/dedup_canonical_headers.py" "$rf" >/dev/null \
      || die 5 "dedup_canonical_headers.py failed on $rf"
    if ! git diff --quiet -- "$rf"; then
      git add "$rf"
      git commit -m "Dedup duplicate canonical headers in $rf" >/dev/null
      echo "    deduped headers in $rf"
    fi
    if ! "$PYTHON3" "$SCRIPT_DIR/dedup_canonical_headers.py" --check "$rf" >/dev/null; then
      die 5 "duplicate canonical headers remain in $rf after dedup."
    fi
  done
fi

# Verify no per-branch contributions were lost. The verifier may
# legitimately report losses (exit 1; e.g. a brand-new section header the
# union-merger does not relocate; see SAPS_II in the 2026-05-20
# consolidation), so that verdict is surfaced as a WARNING for the operator
# to reconcile by hand before opening the PR, rather than killing the
# pipeline outright. Any other non-zero exit means the verifier could not
# run at all, and carrying on would ship an unverified merge.
echo
echo "==> Verifying no per-branch model contributions are missing"
for vf in "${RESTORE_TARGETS[@]:-$UNION_FILE}"; do
  verify_args=(
    --repo "$REPO"
    --branch "$BRANCH_NAME"
    --base "$BASE"
    --pattern "$PATTERN"
    --file "$vf"
    "${EXTRA_REF_ARGS[@]}"
  )
  verify_rc=0
  "$SCRIPT_DIR/verify_branch_contributions.sh" "${verify_args[@]}" || verify_rc=$?
  if (( verify_rc == 1 )); then
    echo "WARNING: verifier reported missing contributions in $vf."
    echo "         Reconcile by hand before opening the PR (the union-merger does"
    echo "         not relocate brand-new section headers, and neither it nor"
    echo "         restore_dropped_sections.py unions two branches' competing"
    echo "         entries for the SAME canonical -- see the placement report)."
  elif (( verify_rc != 0 )); then
    die 5 "verify_branch_contributions.sh could not run on $vf (exit $verify_rc)."
  fi
done

# Union-merge NEWS.md. It has ONE append point ("# development version"), so
# every branch edits the same lines and -X theirs takes the last branch's whole
# copy -- which, being cut from an older main, is missing what main accumulated
# since. The loss is doubly silent: entries already on main are DELETED and
# every other branch's bullet is dropped. On 2026-08-20 NEWS.md came out of the
# merge 85 lines SHORT with not one of the 169 merged models represented; an
# earlier round lost 60. Rebuild from base + every branch's bullets, gated on
# the model actually being shipped by this merge.
if [[ -f NEWS.md ]]; then
  echo
  echo "==> Union-merging NEWS.md"
  "$PYTHON3" "$SCRIPT_DIR/union_merge_news.py" \
    --repo "$REPO" --branch "$BRANCH_NAME" --base "$BASE" --pattern "$PATTERN" \
    "${EXTRA_REF_ARGS[@]}" || die 5 "union_merge_news.py failed on NEWS.md"
  if ! git diff --quiet -- NEWS.md; then
    git add NEWS.md
    git commit -m "Union-merge NEWS.md across all folded branches" >/dev/null
    echo "    committed NEWS.md union"
  fi
fi

# R-side registry regeneration.
if (( ! SKIP_R_REGEN )); then
  echo
  PRE_REGEN_DIRTY="$(git status --porcelain)"
  echo "==> Regenerating registry artifacts (Rscript)"
  if ! Rscript -e '
    suppressPackageStartupMessages(library(devtools))
    cat("--- load_all ---\n")
    load_all(".", quiet = TRUE)
    if (exists("buildModelDb", where = asNamespace("nlmixr2lib"), inherits = FALSE)) {
      cat("--- buildModelDb ---\n")
      nlmixr2lib:::buildModelDb()
    } else {
      cat("--- skipping buildModelDb (function not found in nlmixr2lib namespace) ---\n")
    }
    cat("--- document ---\n")
    document()
    cat("--- done ---\n")
  ' 2>&1 | tail -10; then
    die 5 "the registry regeneration failed (its last output is above). The worktree is left at $WT_ABS"
  fi

  # Stage whatever the regen actually WROTE, not a hardcoded artifact list.
  #
  # The old list named `inst/modeldb.qs2` -- a path this same script carries on
  # its forbidden-resurrection list, because nlmixr2lib dropped it in favour of
  # `inst/modeldb.rds`.  So the staging line asked for a file that cannot exist
  # and never named the one that does: on the 2026-08-31 consolidation the
  # regenerated registry would have been left unstaged and the branch pushed
  # with a registry blob stale against 181 new model files.  A hardcoded list
  # silently under-stages every time the package renames a derived artifact.
  #
  # Every earlier step commits its own work, so the worktree is clean on entry
  # here and anything dirty now is regen output.  Assert that rather than
  # assume it: if the tree was already dirty the operator needs to know, since
  # `git add -A` would sweep unrelated edits into the regen commit.
  if [[ -n "$PRE_REGEN_DIRTY" ]]; then
    echo "    WARN: worktree was already dirty before the regen step; staging"
    echo "          only known artifact paths to avoid committing unrelated edits:"
    printf '%s\n' "$PRE_REGEN_DIRTY" | sed 's/^/            /'
    git add -A _pkgdown.yml data/ inst/ man/ NAMESPACE 2>/dev/null || true
  elif ! git diff --quiet; then
    git add -A
  fi
  if ! git diff --staged --quiet; then
    git commit -m "Regenerate modeldb + man docs + pkgdown navbar after merging $SUCCESS branches" >/dev/null
    echo "    committed regen artifacts: $(git show --stat --format= --name-only HEAD | tr '\n' ' ')"
  fi
fi

# devtools::check pre-push gate.
if (( ! SKIP_CHECK )); then
  echo
  echo "==> Running devtools::check (this can take ~5-15 min)"
  if Rscript -e 'devtools::check(error_on = "error", args = "--no-build-vignettes")' 2>&1 | tail -20; then
    echo "    check passed"
  else
    echo "ERROR: devtools::check failed. The worktree is left in place at" >&2
    echo "  $WT_ABS" >&2
    echo "Fix the failures, re-run check, and push manually when green." >&2
    exit 6
  fi
fi

# Parallel vignette validation pre-push gate.
#
# Why this exists: devtools::check runs with --no-build-vignettes (the
# CarlssonPetri segfault is the on-disk reason), so vignette
# evaluation is NOT covered by step 8. pkgdown's CI runs vignettes
# sequentially and ABORTS on the first failure, so after a large
# merge it surfaces broken vignettes one at a time across many cycles
# — a 14-failure consolidation can take 14 CI iterations to drain.
# A local parallel pass (callr-isolated, continues-on-failure) finds
# them all in one shot. This is a HARD GATE: a failed vignette
# blocks push.
#
# The gate is the validator's own exit status (0 only when every vignette
# rendered, or there were none) plus the per-file results. It used to be the
# results alone, with the pipeline as a bare `if` condition, so an Rscript
# that was missing or died before writing a line -- a failed install, a
# missing package -- left no "ok":false to find and the run went on to push.
if (( ! SKIP_VIGNETTES )); then
  echo
  echo "==> Parallel vignette validation (every Rmd under vignettes/articles/)"
  echo "    jobs=$VIGNETTE_JOBS  timeout=${VIGNETTE_TIMEOUT}s/vignette"
  VIGNETTE_RESULTS="${WT_ABS}/.vignette_results.jsonl"
  VIGNETTE_LOG="${WT_ABS}/.vignette_build.log"
  rm -f "$VIGNETTE_RESULTS"  # never judge this run by an earlier run's results
  vignette_rc=0
  Rscript "$SCRIPT_DIR/verify_vignettes_parallel.R" \
       --worktree "$WT_ABS" \
       --jobs "$VIGNETTE_JOBS" \
       --timeout "$VIGNETTE_TIMEOUT" \
       --results "$VIGNETTE_RESULTS" 2>&1 | tee "$VIGNETTE_LOG" \
       | { grep -E '^\[FAIL|^SUMMARY|^FAILURES' || true; } || vignette_rc=$?
  if (( vignette_rc != 0 )) || grep -q '"ok":false' "$VIGNETTE_RESULTS" 2>/dev/null; then
    echo
    if [[ -s "$VIGNETTE_RESULTS" ]]; then
      echo "ERROR: at least one vignette failed to render (validator exit $vignette_rc). Worktree at" >&2
    else
      echo "ERROR: the vignette validator exited $vignette_rc without recording a result. Its last lines:" >&2
      tail -n 5 "$VIGNETTE_LOG" | sed 's/^/    /' >&2
      echo "Worktree at" >&2
    fi
    echo "  $WT_ABS" >&2
    echo "Full log:    $VIGNETTE_LOG" >&2
    echo "Per-file JSONL: $VIGNETTE_RESULTS" >&2
    echo >&2
    echo "Fix the failing vignettes (or the underlying model .R files, or the" >&2
    echo "validator's setup), re-run validation, and push manually when green:" >&2
    echo "  Rscript $SCRIPT_DIR/verify_vignettes_parallel.R \\" >&2
    echo "    --worktree $WT_ABS \\" >&2
    echo "    --jobs $VIGNETTE_JOBS" >&2
    exit 8
  fi
  echo "    all vignettes rendered cleanly"
fi

# Push.
if (( SKIP_PUSH )); then
  echo
  echo "==> --skip-push set; skipping push."
else
  echo
  echo "==> Pushing $BRANCH_NAME to origin"
  if ! git push -u origin "$BRANCH_NAME" 2>&1 | tail -5; then
    echo "ERROR: push failed." >&2
    exit 7
  fi
fi

# Print PR title + body.
PR_TITLE_LIMIT=70
NEW_MODELS=$(git log --no-merges "$BASE..HEAD" --format='%s' | grep -ciE "Add .* model" || true)
ASCII_FIXES=$(git log --no-merges "$BASE..HEAD" --format='%s' | grep -ciE "ASCII|em-dash|non-ASCII" || true)
OTHER_COMMITS=$(git log --no-merges "$BASE..HEAD" --format='%s' | grep -civE "Add .* model|ASCII|em-dash|non-ASCII" || true)

# Compose a short title.
SUFFIX_BITS=()
[[ "$NEW_MODELS" -gt 0 ]] && SUFFIX_BITS+=("$NEW_MODELS new models")
[[ "$ASCII_FIXES" -gt 0 ]] && SUFFIX_BITS+=("$ASCII_FIXES ASCII fixes")
PR_TITLE="Merge $SUCCESS claude/* branches"
if [[ ${#SUFFIX_BITS[@]} -gt 0 ]]; then
  PR_TITLE="$PR_TITLE ($(IFS=, ; echo "${SUFFIX_BITS[*]}"))"
fi

# Trim title to limit.
if (( ${#PR_TITLE} > PR_TITLE_LIMIT )); then
  PR_TITLE="${PR_TITLE:0:$((PR_TITLE_LIMIT-1))}…"
fi

echo
echo "================================================================"
echo "Suggested PR title (≤${PR_TITLE_LIMIT} chars):"
echo
echo "$PR_TITLE"
echo
echo "Suggested PR body:"
echo
cat <<EOF
## Summary

Consolidates $SUCCESS unmerged \`claude/<task-id>\` branches from the
nlmixr2lib popPK ingestion runner queue into one review-ready
branch.

### Categorical breakdown

- $NEW_MODELS new-model addition(s)
- $ASCII_FIXES vignette ASCII-gate cleanup(s)
- $OTHER_COMMITS other (follow-up edits, model updates, etc.)

### Mechanical regeneration commit

After the merges, ran:

\`\`\`sh
Rscript -e 'devtools::load_all("."); nlmixr2lib:::buildModelDb(); devtools::document()'
\`\`\`

to canonically rebuild \`data/modeldb.rda\`, \`inst/modeldb.qs2\`, the
\`_pkgdown.yml\` navbar, and \`man/modeldb.Rd\` from all model \`.R\`
files now on the branch.

### Procedural note for future merges

The merge strategy was \`-X theirs\` for binary registry files +
metadata. This strategy clobbers \`$UNION_FILE\` because multiple
branches independently rewrite the same \`**Example models:**\`
lines. The script's union-merger step reconstructs the file by
parsing each branch's diff and unioning per-model annotations.
**Do not skip the union-merge step on future runs.**

### Per-task tracking after this PR merges

Each source \`claude/<task-id>\` branch was folded in with a real
\`git merge\`, so its tip is a true ancestor of this branch. Once
this PR lands on main, every consolidated branch is reported as
merged by \`git branch --merged origin/main\` and GitHub marks each
as "Merged" automatically — no force-advance step required. The
source branches can then be deleted at the operator's discretion
(\`git push --delete origin claude/<task-id>\`).

## Test plan

- [ ] \`devtools::check(error_on = "error", args = "--no-build-vignettes")\` (already run; passed pre-push)
- [ ] \`nlmixr2lib::modellib()\` lists all newly-added models
- [ ] Spot-check one of the new models: \`nlmixr2lib::readModelDb(name = "<one>")\` returns a function
- [ ] Rendered pkgdown navbar shows the new vignettes under the right section

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
echo
echo "================================================================"
echo
echo "Open the PR via:"
echo "  https://github.com/<org>/<repo>/pull/new/${BRANCH_NAME}"
echo
echo "Worktree left at: $WT_ABS"
echo
echo "Source branches were folded in with real merges, so after this PR"
echo "lands on main they show as \"Merged\" automatically (git branch"
echo "--merged origin/main lists them). No post-merge advance step needed."
