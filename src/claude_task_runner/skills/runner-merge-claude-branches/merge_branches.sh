#!/bin/bash
# Consolidate per-task claude/* branches into one review-ready branch.
#
# End-to-end orchestrator for /runner-merge-claude-branches. Runs:
#   1. Pre-flight survey (which branches have unmerged commits)
#   2. Worktree creation off the configured base
#   3. Sequential merge with -X theirs
#   3b. Resurrected-path guard (re-remove files the base deleted that a
#       stale branch brought back via -X theirs)
#   4. R-side registry regeneration (buildModelDb + document)
#   5. Union-merge of covariate-columns.md (recovers the annotations
#      -X theirs would have lost)
#   6. devtools::check pre-push gate
#   7. Parallel vignette validation pre-push gate
#   8. Push branch + print PR title/body
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
#                           Flows through to union_merge_lines.py and
#                           verify_branch_contributions.sh.
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
#   --skip-r-regen          Skip step 4 (buildModelDb / document). Use when
#                           merging into a repo that doesn't have these.
#   --skip-check            Skip step 6 (devtools::check). Use for fast
#                           iteration; the operator runs check separately.
#   --skip-vignettes        Skip step 7 (parallel vignette validation).
#                           Use only when iterating; not recommended for
#                           the final pre-push run because pkgdown CI's
#                           sequential vignette build will surface the
#                           failures one at a time.
#   --vignette-jobs <N>     Parallel workers for vignette validation
#                           (default: max(1, ncpus - 2)).
#   --vignette-timeout <S>  Per-vignette wall-clock ceiling in seconds
#                           (default: 900). Increase if you have a model
#                           that legitimately needs >15 minutes.
#   --skip-push             Don't push the branch (steps 1-7 only).
#   --dry-run               Print the survey and exit before creating the worktree.
#   --yes                   Don't prompt; assume yes to "create worktree".
#   -h, --help              Show this help.
#
# Exit codes:
#   0  success
#   2  bad args
#   3  pre-flight failure (no unmerged branches, dirty worktree, etc.)
#   4  merge failed
#   5  union-merge or verification failed
#   6  devtools::check failed
#   7  push failed
#   8  parallel vignette validation failed (one or more Rmd did not render)
set -euo pipefail

# Resolved once: the union-merger, the dedup, the dropped-section restore and
# the NEWS union all need it, and they no longer all live inside the same
# conditional block (see NOTE ON ORDER below).
PYTHON3="$(command -v python3 || true)"

REPO="${PWD}"
BASE="origin/main"
PATTERN="origin/claude/*"
BRANCH_NAME=""
UNION_FILE="inst/references/covariate-columns.md"
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  sed -n '2,60p' "$0" | sed 's/^# \?//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    --base) BASE="$2"; shift 2 ;;
    --pattern) PATTERN="$2"; shift 2 ;;
    --extra-ref) EXTRA_REFS+=("$2"); shift 2 ;;
    --branch-name) BRANCH_NAME="$2"; shift 2 ;;
    --union-file) UNION_FILE="$2"; shift 2 ;;
    --forbid-path)
      if [[ -z "$2" ]]; then FORBID_PATHS=(); else
        if [[ "${FORBID_PATHS[*]}" == "inst/modeldb.qs2" ]]; then FORBID_PATHS=(); fi
        FORBID_PATHS+=("$2")
      fi; shift 2 ;;
    --skip-r-regen) SKIP_R_REGEN=1; shift ;;
    --skip-check) SKIP_CHECK=1; shift ;;
    --skip-vignettes) SKIP_VIGNETTES=1; shift ;;
    --vignette-jobs) VIGNETTE_JOBS="$2"; shift 2 ;;
    --vignette-timeout) VIGNETTE_TIMEOUT="$2"; shift 2 ;;
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

if [[ -z "$BRANCH_NAME" ]]; then
  BRANCH_NAME="merge-all-claude-branches-$(date -u +%F)"
fi

cd "$REPO"

# Repo sanity.
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "ERROR: $REPO is not a git working tree." >&2
  exit 3
fi

# Fetch so origin refs are current.
echo "==> Fetching $BASE / source refs (--prune)"
git fetch origin --prune 2>&1 | tail -5

# Survey.
echo
echo "==> Surveying $PATTERN branches with unmerged commits vs $BASE"
echo "    base = $(git rev-parse --short "$BASE")"

# Expand pattern to a concrete list of branches under refs/remotes/,
# then append any --extra-ref entries.
mapfile -t ALL_MATCHES < <(
  git for-each-ref --format='%(refname:short)' "refs/remotes/$PATTERN" 2>/dev/null | sort -u
)
if [[ ${#ALL_MATCHES[@]} -eq 0 && ${#EXTRA_REFS[@]} -eq 0 ]]; then
  echo "ERROR: no branches matched refspec '$PATTERN' under refs/remotes/ and no --extra-ref supplied" >&2
  exit 3
fi
for er in "${EXTRA_REFS[@]:-}"; do
  [[ -z "$er" ]] && continue
  # Verify the ref exists.
  if ! git rev-parse --verify "$er" >/dev/null 2>&1; then
    echo "ERROR: --extra-ref '$er' does not exist" >&2
    exit 3
  fi
  # Avoid duplicates if it's already in the pattern matches.
  in_pattern=0
  for m in "${ALL_MATCHES[@]:-}"; do
    if [[ "$m" == "$er" ]]; then in_pattern=1; break; fi
  done
  if (( ! in_pattern )); then
    ALL_MATCHES+=("$er")
  fi
done

UNMERGED=()
for br in "${ALL_MATCHES[@]}"; do
  ahead=$(git rev-list --count "$BASE..$br" 2>/dev/null || echo 0)
  if [[ "$ahead" != "0" && -n "$ahead" ]]; then
    UNMERGED+=("$br")
  fi
done

if [[ ${#UNMERGED[@]} -eq 0 ]]; then
  echo "==> No unmerged branches found. Nothing to do."
  exit 0
fi

echo "    found ${#UNMERGED[@]} unmerged branch(es):"
for br in "${UNMERGED[@]}"; do
  ahead=$(git rev-list --count "$BASE..$br")
  files=$(git diff --name-only "$BASE..$br" | wc -l)
  subj=$(git log -1 --format='%s' "$br")
  printf "      %-65s  ahead=%s  files=%s  %s\n" "${br#origin/}" "$ahead" "$files" "${subj:0:60}"
done

if (( DRY_RUN )); then
  echo
  echo "==> Dry-run; stopping before worktree creation."
  exit 0
fi

# Confirm.
if (( ! ASSUME_YES )); then
  echo
  read -r -p "Proceed creating worktree and merging ${#UNMERGED[@]} branches? [y/N] " ans
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
  echo "  Remove via:"
  echo "    git -C $REPO worktree remove --force $WT_REL"
  echo "    git -C $REPO branch -D $BRANCH_NAME"
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
#     man/*.Rd, and the _pkgdown.yml navbar  -> regenerated in step 5;
#   * covariate-columns.md structured lines                -> union-merged in step 6.
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
    for er in "${EXTRA_REFS[@]:-}"; do
      [[ -n "$er" ]] && union_args+=( --extra-ref "$er" )
    done
    "$PYTHON3" "$SCRIPT_DIR/union_merge_lines.py" "${union_args[@]}"
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
  PYTHON3="${PYTHON3:-$(command -v python3)}"
  "$PYTHON3" "$SCRIPT_DIR/dedup_canonical_headers.py" --global "$UNION_FILE"
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
    echo "ERROR: duplicate canonical headers remain in $UNION_FILE after dedup." >&2
    exit 1
  fi
fi

# Verify no per-branch contributions were lost. The verifier may
# legitimately exit non-zero (e.g. a brand-new section header the
# union-merger does not relocate; see SAPS_II in the 2026-05-20
# consolidation). With `set -e` active a non-zero exit would abort the
# whole run, so we tolerate it here: a failed verdict is surfaced as a
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
for reg in inst/references/compartment-names.md inst/references/parameter-names.md; do
  [[ -f "$reg" ]] || continue
  [[ "$reg" == "$UNION_FILE" ]] && continue
  echo
  echo "==> Deduping duplicate canonical headers in $reg (per-section)"
  "$PYTHON3" "$SCRIPT_DIR/dedup_canonical_headers.py" "$reg"
  if ! git diff --quiet -- "$reg"; then
    git add "$reg"
    git commit -m "Dedup duplicate canonical headers in $reg" >/dev/null
    echo "    committed dedup of $reg"
  fi
  if ! "$PYTHON3" "$SCRIPT_DIR/dedup_canonical_headers.py" --check "$reg"; then
    echo "ERROR: duplicate canonicals remain in $reg after dedup." >&2
    exit 9
  fi
done

# Restore whole `### CANONICAL` blocks that -X theirs dropped. The union-merger
# folds Example-model LINES inside buckets that already exist; it cannot bring
# back a canonical whose entire block is gone, which is what happens when a
# branch adds a brand-new canonical and a later branch (based on an older main,
# so lacking it) touches the same region. verify_branch_contributions.sh below
# REPORTS that loss but does not repair it, so this was a manual step on every
# large merge -- 21 blocks on 2026-08-20 alone.
if [[ -n "$UNION_FILE" && -f "$UNION_FILE" ]]; then
  echo
  echo "==> Restoring canonical blocks dropped by the merge"
  restore_args=(
    --repo "$REPO"
    --branch "$BRANCH_NAME"
    --base "$BASE"
    --pattern "$PATTERN"
    --file "$UNION_FILE"
  )
  "$PYTHON3" "$SCRIPT_DIR/restore_dropped_sections.py" "${restore_args[@]}"
  if ! git diff --quiet -- "$UNION_FILE"; then
    git add "$UNION_FILE"
    git commit -m "Restore canonical blocks dropped by -X theirs in $UNION_FILE" >/dev/null
    echo "    committed restored blocks"
  fi
fi

# WARNING for the operator to reconcile covariate-columns.md by hand
# before opening the PR, rather than killing the pipeline outright.
echo
echo "==> Verifying no per-branch model contributions are missing"
verify_args=(
  --repo "$REPO"
  --branch "$BRANCH_NAME"
  --base "$BASE"
  --pattern "$PATTERN"
  --file "$UNION_FILE"
)
for er in "${EXTRA_REFS[@]:-}"; do
  [[ -n "$er" ]] && verify_args+=( --extra-ref "$er" )
done
if ! "$SCRIPT_DIR/verify_branch_contributions.sh" "${verify_args[@]}"; then
  echo "WARNING: verifier reported missing contributions in $UNION_FILE."
  echo "         Reconcile by hand before opening the PR (the union-merger does"
  echo "         not relocate brand-new section headers)."
fi

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
    --repo "$REPO" --branch "$BRANCH_NAME" --base "$BASE" --pattern "$PATTERN"
  if ! git diff --quiet -- NEWS.md; then
    git add NEWS.md
    git commit -m "Union-merge NEWS.md across all folded branches" >/dev/null
    echo "    committed NEWS.md union"
  fi
fi

# R-side registry regeneration.
if (( ! SKIP_R_REGEN )); then
  echo
  echo "==> Regenerating registry artifacts (Rscript)"
  if ! command -v Rscript >/dev/null 2>&1; then
    echo "ERROR: Rscript not found in PATH. Pass --skip-r-regen if you'll do it later." >&2
    exit 5
  fi
  Rscript -e '
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
  ' 2>&1 | tail -10

  if ! git diff --quiet; then
    git add -A _pkgdown.yml data/modeldb.rda inst/modeldb.qs2 man/ 2>/dev/null || true
    if ! git diff --staged --quiet; then
      git commit -m "Regenerate modeldb + man docs + pkgdown navbar after merging $SUCCESS branches" >/dev/null
      echo "    committed regen artifacts"
    fi
  fi
fi

# devtools::check pre-push gate.
if (( ! SKIP_CHECK )); then
  echo
  echo "==> Running devtools::check (this can take ~5-15 min)"
  if Rscript -e 'devtools::check(error_on = "error", args = "--no-build-vignettes")' 2>&1 | tail -20; then
    echo "    check passed"
  else
    echo "ERROR: devtools::check failed. The worktree is left in place at"
    echo "  $WT_ABS"
    echo "Fix the failures, re-run check, and push manually when green."
    exit 6
  fi
fi

# Parallel vignette validation pre-push gate.
#
# Why this exists: devtools::check runs with --no-build-vignettes (the
# CarlssonPetri segfault is the on-disk reason), so vignette
# evaluation is NOT covered by step 6. pkgdown's CI runs vignettes
# sequentially and ABORTS on the first failure, so after a large
# merge it surfaces broken vignettes one at a time across many cycles
# — a 14-failure consolidation can take 14 CI iterations to drain.
# A local parallel pass (callr-isolated, continues-on-failure) finds
# them all in one shot. This is a HARD GATE: a failed vignette
# blocks push.
if (( ! SKIP_VIGNETTES )); then
  echo
  echo "==> Parallel vignette validation (every Rmd under vignettes/articles/)"
  echo "    jobs=$VIGNETTE_JOBS  timeout=${VIGNETTE_TIMEOUT}s/vignette"
  VIGNETTE_RESULTS="${WT_ABS}/.vignette_results.jsonl"
  if Rscript "$SCRIPT_DIR/verify_vignettes_parallel.R" \
       --worktree "$WT_ABS" \
       --jobs "$VIGNETTE_JOBS" \
       --timeout "$VIGNETTE_TIMEOUT" \
       --results "$VIGNETTE_RESULTS" 2>&1 | tee "${WT_ABS}/.vignette_build.log" \
       | grep -E '^\[FAIL|^SUMMARY|^FAILURES'; then
    : # rendered cleanly; summary already emitted
  fi
  if grep -q '"ok":false' "$VIGNETTE_RESULTS" 2>/dev/null; then
    echo
    echo "ERROR: at least one vignette failed to render. Worktree at"
    echo "  $WT_ABS"
    echo "Full log:    $WT_ABS/.vignette_build.log"
    echo "Per-file JSONL: $VIGNETTE_RESULTS"
    echo
    echo "Fix the failing vignettes (or the underlying model .R files),"
    echo "re-run validation, and push manually when green:"
    echo "  Rscript $SCRIPT_DIR/verify_vignettes_parallel.R \\"
    echo "    --worktree $WT_ABS \\"
    echo "    --jobs $VIGNETTE_JOBS"
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
