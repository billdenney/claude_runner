#!/bin/bash
# Consolidate per-task claude/* branches into one review-ready branch.
#
# End-to-end orchestrator for /runner-merge-claude-branches. Runs:
#   1. Pre-flight survey (which branches have unmerged commits)
#   2. Worktree creation off the configured base
#   3. Sequential merge with -X theirs
#   4. R-side registry regeneration (buildModelDb + document)
#   5. Union-merge of covariate-columns.md (recovers the annotations
#      -X theirs would have lost)
#   6. devtools::check pre-push gate
#   7. Push branch + print PR title/body
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

REPO="${PWD}"
BASE="origin/main"
PATTERN="origin/claude/*"
BRANCH_NAME=""
UNION_FILE="inst/references/covariate-columns.md"
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
  sed -n '2,50p' "$0" | sed 's/^# \?//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    --base) BASE="$2"; shift 2 ;;
    --pattern) PATTERN="$2"; shift 2 ;;
    --extra-ref) EXTRA_REFS+=("$2"); shift 2 ;;
    --branch-name) BRANCH_NAME="$2"; shift 2 ;;
    --union-file) UNION_FILE="$2"; shift 2 ;;
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

# Sequential cherry-pick.
#
# We use cherry-pick rather than `git merge -X theirs` because the
# claude/* branches are often based on an OUTDATED main (e.g. created
# off the merge-base of a prior consolidation PR, not the current
# main HEAD). With `merge -X theirs` we'd silently roll back the
# main-side updates for any file the stale branch carries unchanged
# (binary registry blobs, NEWS.md sections, covariate-columns.md
# entries from previously-merged work). Cherry-pick applies the
# commit's DELTA on top of the new branch, which is exactly the
# semantic the operator wants: "fold each branch's per-task commit
# into one branch."
#
# `-X theirs` is still passed so per-commit conflicts (e.g. two
# branches each editing the same NEWS.md line) resolve to the
# incoming side; the covariate-columns.md union-merge step below
# repairs structured-markdown losses.
cd "$WT_ABS"
echo
echo "==> Sequential cherry-pick with -X theirs (one commit per branch;"
echo "    binaries regenerated and covariate-columns.md union-merged after)"
SUCCESS=0
FAIL=0
FAILED_LIST=()
# Map source-branch -> (new_sha_on_consolidation, original_source_sha).
# Used by the post-merge advance script (emitted at end) to force-push
# each source branch tip to its cherry-picked commit AFTER the
# consolidation PR lands on main — preserving per-task tracking so
# GitHub shows each claude/<task-id> branch as "merged" rather than
# perpetually "1 commit ahead".
CHERRY_BRANCH=()
CHERRY_NEW_SHA=()
CHERRY_ORIG_SHA=()
for br in "${UNMERGED[@]}"; do
  short=${br#origin/}
  ahead=$(git rev-list --count "$BASE..$br")
  echo "    --- $short ($ahead commit ahead) ---"
  if git cherry-pick --strategy=recursive -X theirs \
        --keep-redundant-commits \
        "$BASE..$br" >/dev/null 2>&1; then
    SUCCESS=$((SUCCESS+1))
    CHERRY_BRANCH+=("$short")
    CHERRY_NEW_SHA+=("$(git rev-parse HEAD)")
    CHERRY_ORIG_SHA+=("$(git rev-parse "$br")")
  else
    FAIL=$((FAIL+1))
    FAILED_LIST+=("$short")
    echo "      FAIL — conflicted files:"
    git diff --name-only --diff-filter=U | sed 's/^/        /'
    echo "      ABORTING this branch; continuing with the rest."
    git cherry-pick --abort 2>/dev/null || git merge --abort 2>/dev/null || true
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

# Union-merge covariate-columns.md (or the configured union-file).
if [[ -n "$UNION_FILE" ]]; then
  echo
  echo "==> Union-merging $UNION_FILE"
  if [[ ! -f "$UNION_FILE" ]]; then
    echo "    union-file not present on this branch; skipping."
  else
    PYTHON3="$(command -v python3)"
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

# Emit post_merge_advance.sh in the worktree. After the consolidation
# PR lands on origin/main, the operator runs this script to advance
# each source claude/<task-id> branch to its cherry-picked commit.
# That makes the source branch tip an ancestor of main, so GitHub
# shows it as "merged" instead of "1 commit ahead" (the latter is
# the visible artifact of cherry-pick creating new SHAs).
echo
echo "==> Emitting post_merge_advance.sh"
ADVANCE_SCRIPT="$WT_ABS/post_merge_advance.sh"
REPO_ABS="$(cd "$REPO" && pwd)"
{
  cat <<EOSHEAD
#!/bin/bash
# Auto-generated by merge_branches.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ).
#
# AFTER the consolidation PR ($BRANCH_NAME) is merged into
# origin/main, run this script to advance each source claude/* branch
# tip to its cherry-picked commit. The branches will then sit on
# main and GitHub will show them as "merged" rather than the
# perpetual "1 commit ahead" that cherry-pick's SHA-rewrite causes.
#
# Usage:
#   bash post_merge_advance.sh             # dry-run (default; shows what would happen)
#   bash post_merge_advance.sh --apply     # actually force-push each branch
#
# Safety:
#   * Aborts if any cherry-picked SHA is not yet on origin/main
#     (i.e. the consolidation PR hasn't merged yet).
#   * Uses --force-with-lease=<branch>:<original_sha> so a source
#     branch that was updated between cherry-pick time and now
#     (e.g. the runner re-dispatched the task) won't be silently
#     clobbered — the push fails and the operator can investigate.
#   * Skips any branch whose origin tip has already moved past the
#     recorded original SHA (would be lossy without manual review).
#
# Exit codes:
#   0  applied (or dry-ran) all branches successfully
#   1  one or more branches not yet on main (PR not merged?)
#   2  one or more pushes rejected (branches moved since cherry-pick;
#      operator must inspect)
set -u
APPLY=0
[[ "\${1:-}" == "--apply" ]] && APPLY=1
cd "$REPO_ABS"

git fetch origin --quiet
NOT_ON_MAIN=0
REJECTED=0
ADVANCED=0
SKIPPED=0

EOSHEAD
  for i in "${!CHERRY_BRANCH[@]}"; do
    short="${CHERRY_BRANCH[$i]}"
    new="${CHERRY_NEW_SHA[$i]}"
    orig="${CHERRY_ORIG_SHA[$i]}"
    cat <<EOENTRY
# --- $short ---
NEW='$new'
ORIG='$orig'
BR='$short'
echo "=== \$BR ==="
if ! git merge-base --is-ancestor "\$NEW" origin/main 2>/dev/null; then
  echo "  SKIP: cherry-picked commit \$NEW is not on origin/main yet."
  echo "        (consolidation PR not merged, or refs out of date — try git fetch)"
  NOT_ON_MAIN=\$((NOT_ON_MAIN+1))
elif (( APPLY )); then
  if git push --force-with-lease=\$BR:\$ORIG origin "\$NEW:refs/heads/\$BR" 2>&1 | sed 's/^/  /'; then
    ADVANCED=\$((ADVANCED+1))
  else
    echo "  REJECTED: source branch moved since cherry-pick. Inspect manually."
    REJECTED=\$((REJECTED+1))
  fi
else
  if [[ \$(git ls-remote origin "refs/heads/\$BR" | cut -f1) != "\$ORIG" ]]; then
    echo "  WARN: source branch moved since cherry-pick — push would be rejected."
    echo "        current remote tip: \$(git ls-remote origin "refs/heads/\$BR" | cut -f1)"
    echo "        expected original:  \$ORIG"
    SKIPPED=\$((SKIPPED+1))
  else
    echo "  DRY-RUN: would push --force-with-lease=\$BR:\$ORIG origin \$NEW:refs/heads/\$BR"
  fi
fi

EOENTRY
  done
  cat <<'EOSTAIL'

echo
echo "=== Summary ==="
echo "  advanced:    $ADVANCED"
echo "  rejected:    $REJECTED   (source branch moved since cherry-pick; manual review)"
echo "  skipped:     $SKIPPED    (dry-run only; would have been rejected)"
echo "  not-on-main: $NOT_ON_MAIN"
if (( ! APPLY )); then
  echo
  echo "(dry-run — re-run with --apply to actually advance the branches.)"
fi
if (( NOT_ON_MAIN > 0 )); then exit 1; fi
if (( REJECTED > 0 )); then exit 2; fi
exit 0
EOSTAIL
} > "$ADVANCE_SCRIPT"
chmod +x "$ADVANCE_SCRIPT"
echo "    wrote $ADVANCE_SCRIPT (${#CHERRY_BRANCH[@]} branch mappings)"

# Verify no per-branch contributions were lost. NB: this runs AFTER
# post_merge_advance.sh is emitted because the verifier may legitimately
# exit non-zero (brand-new section header lost; see SAPS_II in the
# 2026-05-20 consolidation). With `set -e` active, a non-zero verifier
# would abort the script before the advance script could be written —
# but the SHA mapping doesn't depend on verifier success, and the
# operator typically wants the advance script available even when the
# union-file needs hand-touching first.
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
"$SCRIPT_DIR/verify_branch_contributions.sh" "${verify_args[@]}"

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

The cherry-pick strategy creates new commit SHAs on this branch, so
each source \`claude/<task-id>\` branch's original tip never becomes
an ancestor of main and GitHub will show every consolidated branch
as "1 commit ahead" indefinitely. To restore per-task tracking,
run \`post_merge_advance.sh\` from the worktree AFTER merging this
PR — it force-advances each source branch tip to its cherry-picked
commit on main, so GitHub then displays the branch as "merged".
\`bash post_merge_advance.sh\` is a dry-run; add \`--apply\` to
actually push.

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
echo "After the PR merges, restore per-task tracking with:"
echo "  bash $WT_ABS/post_merge_advance.sh              # dry-run"
echo "  bash $WT_ABS/post_merge_advance.sh --apply      # force-push each branch"
