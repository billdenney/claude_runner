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
#      verifier: a loss it reports goes into the hand-union report and stops
#      the run before the push (step 10), unless --accept-verifier-losses.
#      Every repair and verify step reads only the merge set: the branches
#      this run merged, each at the commit it merged (merge_set.py).
#   6. Union-merge of NEWS.md, then the revert check: register content the
#      base has that the merge lost and no merged branch removed stops the
#      run (verify_no_base_reverts.py)
#   7. R-side registry regeneration (buildModelDb + document)
#   8. devtools::check pre-push gate
#   9. Parallel vignette validation pre-push gate
#  10. Push branch + print PR title/body. When a repair set wording aside
#      (copies of a canonical worded differently, a model annotated two ways,
#      a canonical two branches added in different words), or the verifier
#      reported a loss, the run stops first (exit 9): the register keeps one
#      wording, the others and the losses are in a hand-union report, and
#      after the hand union is committed --finish --hand-union-done pushes
#      and prints the PR text from the same worktree, once the gates pass
#      again. --finish completes any run whose repairs completed: one that
#      stopped for a hand union (exit 9), at the revert check (5), a failed
#      regeneration (5), check (6), push (7) or vignette run (8), or was
#      interrupted after its repairs; each of those prints the --finish
#      command, and every failure names a pending report. A run that
#      stopped before its repairs completed (a repair that crashed,
#      duplicates left after the dedup, a verifier that could not run)
#      cannot be completed: re-run it on a fresh --branch-name.
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
#   --accept-verifier-losses
#                           Accept the models, headers or source aliases the
#                           contribution verifier reports missing from a
#                           register. With --finish: push although the
#                           verifier, run again on the branch, still reports
#                           losses; a branch it finds whole needs no flag. On
#                           a run: print a WARNING for each instead of
#                           recording it and stopping before the push.
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
#   --finish                Complete a run that stopped (a hand union, exit 9,
#                           or a failed gate) once the fix is committed: run the
#                           gates again on the worktree's branch, run every R
#                           step the run did not complete, push, and print the
#                           PR text. Takes the run's --repo and
#                           --branch-name (every stop prints them) and reads
#                           the run's other inputs -- --base, --pattern,
#                           --extra-ref, --union-file and the register files
#                           -- from the worktree, refusing a conflicting one
#                           given here (exit 2). Its gates read every ref the
#                           run merged at the commit it merged, so a ref
#                           rewritten or pruned since changes nothing; a
#                           recorded commit missing from the objects stops it
#                           (exit 3). It refuses
#                           a worktree whose run stopped before its repairs
#                           completed (exit 3), and one with uncommitted
#                           changes or untracked files, listing them. It
#                           prints a pending hand-union report and stops
#                           (exit 9) unless --hand-union-done is given;
#                           runs the duplicate check, the revert check and the
#                           contribution verifier, stopping (exit 9) while the
#                           verifier reports a loss unless
#                           --accept-verifier-losses; then runs, each unless
#                           skipped, the regeneration and check if they never
#                           completed or HEAD has moved since they did, and
#                           the vignettes if they never completed or a commit
#                           since touched a file other than the run's
#                           registers. A step the run skipped stays skipped
#                           without the flag being given again; a --skip-*
#                           given to --finish skips that step if it is due.
#   --hand-union-done       With --finish: the hand-union report has been
#                           worked through -- its wording unioned into the
#                           register and committed, any contribution it lists
#                           as missing reconciled -- so --finish goes on to
#                           its gates rather than stopping at the report.
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
#   2  bad arguments: an unknown flag, a flag missing its value, a
#      non-numeric --vignette-jobs / --vignette-timeout, or, for --finish,
#      an input that conflicts with the one the run recorded
#   3  pre-flight failure: --repo is not a git working tree, python3 missing,
#      Rscript missing while an R step is enabled, git fetch failed, --base or
#      an --extra-ref does not resolve, no branch matched, a survey diff
#      failed (no merge base, or a shallow clone), no --yes without a
#      terminal, or the worktree already exists; for --finish, no worktree,
#      a worktree on another branch, one with uncommitted changes or
#      untracked files, no recorded inputs, a run that stopped before its
#      repairs completed, a merged commit missing from the objects, or no
#      Rscript while an R step is still pending
#   4  nothing merged: two branches add one path with different content (the
#      survey stops before merging), or every merge failed
#   5  a repair or verification step failed: a union-merge, dedup or restore
#      script crashed, duplicate canonical headers survived dedup (or a
#      register on --base is missing from the worktree at --finish), the
#      verifier could not run, the revert check found base content the merge
#      reverted (or could not run), or the R registry regeneration failed
#   6  devtools::check failed
#   7  push failed
#   8  parallel vignette validation failed, or the validator could not run
#   9  a hand union is needed: a repair set wording aside, or the verifier
#      reported a contribution missing, and every step but the push ran; the
#      message names the worktree, the report of what the register lacks, and
#      the --finish command to run once the hand union is committed. --finish
#      also exits 9 while that report holds anything and --hand-union-done is
#      not given, or while the verifier it runs still reports a loss and
#      --accept-verifier-losses is not given.
set -Eeuo pipefail
# errtrace (-E): functions and subshells inherit the ERR trap, so a command
# that fails inside a function is reported and exits 1 like one at the top
# level. In a subshell (a command substitution, a pipeline stage) the trap
# exits with the failing status and leaves the message to the shell that
# started it, so a failure is reported once, with that status. A command
# substitution therefore stops at its first failing command, not only its last.
trap 'err_rc=$?; (( BASH_SUBSHELL == 0 )) || exit "$err_rc"; echo "ERROR: unexpected failure (exit $err_rc) at line $LINENO" >&2; exit 1' ERR
trap 'remind_hand_union $?' EXIT
# An interrupt or a kill exits with its own status, so the EXIT trap names a
# pending report then too, rather than seeing the status of the last command.
trap 'interrupted 130' INT
trap 'interrupted 143' TERM
HAND_UNION_REPORT=""
# Which of steps 7-9 the run skipped, and the commit each completed at
# (stage_get); kept in the worktree's git directory for --finish.
STAGES_FILE=""
# The gates' inputs and the refs the run merged (record_inputs), for --finish.
INPUTS_FILE=""
MERGED_REFS=()
# With --finish, the gates read the merge set the run recorded (--merged-record).
RECORD_ARGS=()
BASE_GIVEN=0
PATTERN_GIVEN=0
UNION_FILE_GIVEN=0
REGISTERS_GIVEN=0
VERIFY_LOSS_FILES=()
HAND_UNION_DONE=0

# Resolved once: the union-merger, the dedup, the dropped-section restore and
# the NEWS union all need it, and they no longer all live inside the same
# conditional block (see NOTE ON ORDER below).
PYTHON3="$(command -v python3 || true)"

REPO="${PWD}"
BASE="origin/main"
PATTERN="origin/claude/*"
BRANCH_NAME=""
DEFAULT_UNION_FILE="inst/references/covariate-columns.md"
UNION_FILE="$DEFAULT_UNION_FILE"
# Register files that are NOT the Example-models union file but are still
# structured '### CANONICAL' registers that -X theirs can silently gut.  The
# 2026-08-31 consolidation had 23 branches touching compartment-names.md and 11
# touching parameter-names.md while every repair ran only against UNION_FILE;
# 11 canonical blocks were lost.  These get restore + dedup + placement checks.
DEFAULT_REGISTER_FILES=("inst/references/compartment-names.md" "inst/references/parameter-names.md")
REGISTER_FILES=("${DEFAULT_REGISTER_FILES[@]}")
FORBID_PATHS=("inst/modeldb.qs2")
SKIP_R_REGEN=0
SKIP_CHECK=0
SKIP_VIGNETTES=0
VIGNETTE_JOBS=""
DEFAULT_VIGNETTE_TIMEOUT=900
VIGNETTE_TIMEOUT=$DEFAULT_VIGNETTE_TIMEOUT
VIGNETTE_JOBS_GIVEN=0
SKIP_PUSH=0
ACCEPT_VERIFIER_LOSSES=0
FINISH=0
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

interrupted() {  # <status>: INT/TERM; says how to complete the run when --finish can
  if [[ -n "$STAGES_FILE" && -f "$STAGES_FILE" ]] && [[ "$(stage_get repairs)" != pending ]]; then
    {
      echo
      echo "Interrupted. The repairs had completed: commit or discard any partial output"
      echo "in $WT_ABS, then run"
      echo "     $(finish_command)"
    } >&2
  fi
  exit "$1"
}

remind_hand_union() {  # EXIT trap: a failure after a repair set wording aside says so
  local rc=$1
  # --hand-union-done: the operator has already unioned the report's wording.
  if (( rc == 0 || rc == 9 || HAND_UNION_DONE )) \
    || [[ -z "$HAND_UNION_REPORT" || ! -s "$HAND_UNION_REPORT" ]]; then
    return 0
  fi
  if [[ -n "$STAGES_FILE" && -f "$STAGES_FILE" ]] && [[ "$(stage_get repairs)" != pending ]]; then
    {
      echo
      echo "NOTE: a hand union is still pending. The wording the register lacks is in"
      echo "    $HAND_UNION_REPORT"
      echo "  Union it into the register and commit before the push; the report goes"
      echo "  with the worktree (git worktree remove), so copy it elsewhere to keep it."
    } >&2
  else
    {
      echo
      echo "NOTE: this run's repairs did not complete, so --finish cannot complete it;"
      echo "  re-run on a fresh --branch-name, copying its hand-union report first if you"
      echo "  want it:"
      echo "    $HAND_UNION_REPORT"
    } >&2
  fi
}

run_dedup() {  # <register> [flags]: fix duplicates, their differing wording to the report
  local file=$1 base_copy
  shift
  # The register as the base has it: of a canonical's copies, the dedup keeps
  # the one the base has, so the merge reverts none of the base's wording.
  base_copy="$(git rev-parse --path-format=absolute --git-path dedup-base-copy.md)"
  git show "$BASE:$file" > "$base_copy" 2>/dev/null || : > "$base_copy"
  "$PYTHON3" "$SCRIPT_DIR/dedup_canonical_headers.py" "$@" --report "$HAND_UNION_REPORT" \
    --prefer "$base_copy" "$file" \
    || die 5 "dedup_canonical_headers.py crashed on $file (exit $?); see its output above."
}

check_dedup() {  # <register> [flags]: dedup --check; exit 1 is the verdict, any other a crash
  local file=$1 rc=0
  shift
  "$PYTHON3" "$SCRIPT_DIR/dedup_canonical_headers.py" "$@" --check "$file" || rc=$?
  if (( rc == 1 )); then
    die 5 "duplicate canonical headers remain in $file."
  elif (( rc != 0 )); then
    die 5 "dedup_canonical_headers.py could not run on $file (exit $rc)."
  fi
}

check_no_duplicates() {  # gate: no duplicate canonical remains in any register
  local reg scope
  for reg in "$UNION_FILE" "${REGISTER_FILES[@]:-}"; do
    [[ -n "$reg" ]] || continue
    scope=()
    if [[ "$reg" == "$UNION_FILE" ]]; then scope=(--global); fi
    if [[ -f "$reg" ]]; then
      check_dedup "$reg" ${scope[@]+"${scope[@]}"}
    elif git cat-file -e "$BASE:$reg" 2>/dev/null; then
      die 5 "$reg is on $BASE but missing from the worktree; restore it before the push."
    else
      echo "    (duplicates) $reg is not in this repository; nothing to check."
    fi
  done
}

finish_command() {  # the --finish that completes this run: the worktree's identity; --finish reads the rest
  local cmd
  cmd="bash $(printf '%q' "$SCRIPT_DIR/merge_branches.sh") --repo $(printf '%q' "$REPO")"
  cmd+=" --branch-name $(printf '%q' "$BRANCH_NAME")"
  if (( VIGNETTE_JOBS_GIVEN )); then cmd+=" --vignette-jobs $VIGNETTE_JOBS"; fi
  if (( VIGNETTE_TIMEOUT != DEFAULT_VIGNETTE_TIMEOUT )); then
    cmd+=" --vignette-timeout $VIGNETTE_TIMEOUT"
  fi
  echo "$cmd --finish"
}

normalise_registers() {  # the union and register paths as git's trees name them (merge_set.normalise_path)
  local normalised line
  normalised="$("$PYTHON3" - "$SCRIPT_DIR" "$UNION_FILE" "${REGISTER_FILES[@]:-}" <<'PY'
import sys

sys.path.insert(0, sys.argv[1])
import merge_set

for path in sys.argv[2:]:
    try:
        print(merge_set.normalise_path(path) if path else "")
    except merge_set.GitError as err:
        sys.exit(f"ERROR: --union-file/--register-file: {err}")
PY
)" || exit 2
  UNION_FILE=""
  REGISTER_FILES=()
  local first=1
  while IFS= read -r line; do
    if (( first )); then UNION_FILE=$line; first=0; elif [[ -n "$line" ]]; then REGISTER_FILES+=("$line"); fi
  done <<< "$normalised"
}

record_inputs() {  # the gates' inputs and every ref merged, at the commit merged
  local item
  {
    printf 'base\t%s\n' "$BASE"
    printf 'pattern\t%s\n' "$PATTERN"
    printf 'union-file\t%s\n' "$UNION_FILE"
    for item in "${EXTRA_REFS[@]:-}"; do
      if [[ -n "$item" ]]; then printf 'extra-ref\t%s\n' "$item"; fi
    done
    for item in "${REGISTER_FILES[@]:-}"; do
      if [[ -n "$item" ]]; then printf 'register-file\t%s\n' "$item"; fi
    done
    for item in "${MERGED_REFS[@]}"; do
      printf 'merged\t%s\t%s\n' "$item" "$(git rev-parse "$item^{commit}")"
    done
  } > "$INPUTS_FILE"
}

same_list() {  # <a> <b>: two newline-separated lists hold the same items
  [[ "$(printf '%s\n' "$1" | sort)" == "$(printf '%s\n' "$2" | sort)" ]]
}

read_inputs() {  # --finish: the run's recorded inputs; a conflicting one given here is refused
  local key value sha base="" pattern="" union="" extras=() registers=() given
  [[ -s "$INPUTS_FILE" ]] \
    || die 3 "--finish: $WT_ABS has no recorded inputs: its run stopped before its merges completed. Re-run on a fresh --branch-name."
  while IFS=$'\t' read -r key value sha; do
    case "$key" in
      base) base=$value ;;
      pattern) pattern=$value ;;
      union-file) union=$value ;;
      extra-ref) extras+=("$value") ;;
      register-file) registers+=("$value") ;;
      merged)
        if ! git cat-file -e "$sha^{commit}" 2>/dev/null; then
          die 3 "--finish: the run merged $value at $sha, which is not in this repository's objects; fetch it, then run --finish again."
        fi ;;
    esac
  done < "$INPUTS_FILE"
  if (( BASE_GIVEN )) && [[ "$BASE" != "$base" ]]; then
    die 2 "--finish uses the run's --base '$base'; --base '$BASE' conflicts with it."
  fi
  if (( PATTERN_GIVEN )) && [[ "$PATTERN" != "$pattern" ]]; then
    die 2 "--finish uses the run's --pattern '$pattern'; --pattern '$PATTERN' conflicts with it."
  fi
  if (( UNION_FILE_GIVEN )) && [[ "$UNION_FILE" != "$union" ]]; then
    die 2 "--finish uses the run's --union-file '$union'; --union-file '$UNION_FILE' conflicts with it."
  fi
  given="$(printf '%s\n' "${EXTRA_REFS[@]:-}")"
  if [[ -n "$given" ]] && ! same_list "$given" "$(printf '%s\n' "${extras[@]:-}")"; then
    die 2 "--finish uses the run's --extra-ref values ($(join_paths "${extras[@]:-}")); those given conflict with them."
  fi
  if (( REGISTERS_GIVEN )) \
    && ! same_list "$(printf '%s\n' "${REGISTER_FILES[@]:-}")" "$(printf '%s\n' "${registers[@]:-}")"; then
    die 2 "--finish uses the run's register files ($(join_paths "${registers[@]:-}")); those given conflict with them."
  fi
  BASE=$base
  PATTERN=$pattern
  UNION_FILE=$union
  EXTRA_REFS=("${extras[@]}")
  REGISTER_FILES=("${registers[@]}")
  # The gates read each merged ref at the commit recorded for it, so a ref
  # rewritten or pruned since changes nothing; an extra ref gone since is left
  # out of the candidates the gates list.
  RECORD_ARGS=(--merged-record "$INPUTS_FILE")
  EXTRA_REF_ARGS=()
  for value in "${EXTRA_REFS[@]:-}"; do
    if [[ -n "$value" ]] && git rev-parse --verify --quiet "$value^{commit}" >/dev/null; then
      EXTRA_REF_ARGS+=(--extra-ref "$value")
    fi
  done
}

join_paths() {  # <path>...: one line, ", " between
  local joined="" path
  for path in "$@"; do joined+="${joined:+, }$path"; done
  echo "$joined"
}

stage_get() {  # <stage>: "pending", "skip", or the commit it completed at
  local stage=$1 name value
  if [[ -f "$STAGES_FILE" ]]; then
    while read -r name value; do
      if [[ "$name" == "$stage" ]]; then
        echo "$value"
        return 0
      fi
    done < "$STAGES_FILE"
  fi
  echo pending
}

stage_set() {  # <stage> <value>
  local stage=$1 value=$2 name old kept=""
  if [[ -f "$STAGES_FILE" ]]; then
    while read -r name old; do
      if [[ "$name" != "$stage" ]]; then kept+="$name $old"$'\n'; fi
    done < "$STAGES_FILE"
  fi
  printf '%s%s %s\n' "$kept" "$stage" "$value" > "$STAGES_FILE"
}

record_skips() {  # the run's --skip-* flags, so --finish keeps them without being told
  stage_set regen "$( (( SKIP_R_REGEN )) && echo skip || echo pending )"
  stage_set check "$( (( SKIP_CHECK )) && echo skip || echo pending )"
  stage_set vignettes "$( (( SKIP_VIGNETTES )) && echo skip || echo pending )"
}

register_targets() {  # the union file and every register file the worktree has
  local rf
  RESTORE_TARGETS=()
  if [[ -n "$UNION_FILE" && -f "$UNION_FILE" ]]; then RESTORE_TARGETS+=("$UNION_FILE"); fi
  for rf in "${REGISTER_FILES[@]:-}"; do
    if [[ -n "$rf" && -f "$rf" ]]; then RESTORE_TARGETS+=("$rf"); fi
  done
}

record_verifier_losses() {  # <register> <verifier output>: into the hand-union report
  local file=$1 out=$2
  {
    echo "# Contributions missing from $file"
    echo
    echo "The contribution verifier reports each model, header or source alias below,"
    echo "which a merged branch added, missing from $file; no repair put it back."
    echo "Reconcile each by hand and commit; --accept-verifier-losses accepts any you"
    echo "leave."
    echo
    echo '````'
    cat "$out"
    echo '````'
    echo
  } >> "$HAND_UNION_REPORT"
}

verify_contributions() {  # <run|finish>: the contribution verifier on every register
  local mode=$1 vf verify_rc out
  out="$(git rev-parse --path-format=absolute --git-path verifier-output.txt)"
  VERIFY_LOSS_FILES=()
  echo
  echo "==> Verifying no per-branch model contributions are missing"
  for vf in "${RESTORE_TARGETS[@]:-$UNION_FILE}"; do
    verify_rc=0
    "$SCRIPT_DIR/verify_branch_contributions.sh" --repo "$REPO" --branch "$BRANCH_NAME" \
      --base "$BASE" --pattern "$PATTERN" --file "$vf" "${EXTRA_REF_ARGS[@]}" \
      ${RECORD_ARGS[@]+"${RECORD_ARGS[@]}"} | tee "$out" || verify_rc=$?
    if (( verify_rc == 1 && ACCEPT_VERIFIER_LOSSES )); then
      echo "WARNING: verifier reported missing contributions in $vf."
      echo "         --accept-verifier-losses: the run goes on. Reconcile them by hand"
      echo "         before opening the PR (see the placement report): no repair"
      echo "         relocates a brand-new section header, or unions two branches'"
      echo "         entries for one canonical filed under different ## sections."
    elif (( verify_rc == 1 )); then
      VERIFY_LOSS_FILES+=("$vf")
      if [[ "$mode" == run ]]; then
        record_verifier_losses "$vf" "$out"
        echo "    (verifier) the contributions missing from $vf are in the hand-union report; the run stops before the push."
      fi
    elif (( verify_rc != 0 )); then
      die 5 "verify_branch_contributions.sh could not run on $vf (exit $verify_rc)."
    fi
  done
}

revert_check() {  # gate: register content the base has that the merge, or a hand edit, lost
  local rf targets=() revert_rc=0
  for rf in "$UNION_FILE" "${REGISTER_FILES[@]:-}"; do
    [[ -n "$rf" ]] || continue
    [[ " ${targets[*]:-} " == *" $rf "* ]] && continue
    if [[ -f "$rf" ]] || git cat-file -e "${BASE}:${rf}" 2>/dev/null; then
      targets+=(--file "$rf")
    fi
  done
  (( ${#targets[@]} )) || return 0
  echo
  echo "==> Checking the registers for base content the merge reverted"
  "$PYTHON3" "$SCRIPT_DIR/verify_no_base_reverts.py" \
    --repo "$REPO" --branch "$BRANCH_NAME" --base "$BASE" --pattern "$PATTERN" \
    "${EXTRA_REF_ARGS[@]}" ${RECORD_ARGS[@]+"${RECORD_ARGS[@]}"} "${targets[@]}" || revert_rc=$?
  if (( revert_rc == 1 )); then
    {
      echo "ERROR: the merge reverted register content $BASE has (listed above). Nothing"
      echo "  was repaired: restore it by hand in $WT_ABS and commit, then run"
      echo "     $(finish_command)"
    } >&2
    exit 5
  elif (( revert_rc != 0 )); then
    die 5 "verify_no_base_reverts.py could not run (exit $revert_rc)."
  fi
}

stop_for_hand_union() {
  if (( ${#VERIFY_LOSS_FILES[@]} )); then
    {
      echo
      echo "STOP: every step but the push ran, but the verifier reported a model, header"
      echo "  or source alias a merged branch added missing from $(join_paths "${VERIFY_LOSS_FILES[@]}"),"
      echo "  and no repair put it back. Each is in the hand-union report, with any"
      echo "  wording a repair set aside:"
      echo "    $HAND_UNION_REPORT"
      echo "  (it goes with the worktree, git worktree remove, so copy it to keep it)."
      echo "  1. In the worktree $WT_ABS, reconcile each by hand, union any wording"
      echo "     set aside into the register, keeping each source's attribution, and"
      echo "     commit."
      echo "  2. Then push the branch and print the PR text. --finish runs the checks"
      echo "     again first, and stops while a loss is left unless you add"
      echo "     --accept-verifier-losses:"
      echo "     $(finish_command) --hand-union-done"
    } >&2
    exit 9
  fi
  {
    echo
    echo "STOP: every step but the push ran, but a repair set wording aside: copies of"
    echo "  a canonical, or of a model's annotation, worded differently. The register"
    echo "  keeps one wording; each other is in"
    echo "    $HAND_UNION_REPORT"
    echo "  (it goes with the worktree, git worktree remove, so copy it to keep it)."
    echo "  1. In the worktree $WT_ABS, union that wording into the register"
    echo "     by hand, keeping each source's attribution, and commit."
    echo "  2. Then push the branch and print the PR text:"
    echo "     $(finish_command) --hand-union-done"
  } >&2
  exit 9
}

push_and_report() {  # push the branch (unless --skip-push) and print the PR text
  if (( SKIP_PUSH )); then
    echo
    echo "==> --skip-push set; skipping push."
  else
    echo
    echo "==> Pushing $BRANCH_NAME to origin"
    if ! git push -u origin "$BRANCH_NAME" 2>&1 | tail -5; then
      {
        echo "ERROR: push failed. Fix the remote or the network, then run"
        echo "     $(finish_command)"
      } >&2
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
  local regen_text check_line vignette_line
  local check_cmd='`devtools::check(error_on = "error", args = "--no-build-vignettes")`'
  if stage_ran regen; then
    regen_text="After the merges, the registry regeneration ran:"
  else
    regen_text="The registry regeneration was $(stage_skip_note regen); run it before merging:"
  fi
  if stage_ran check; then
    check_line="- [x] $check_cmd passed before the push"
  else
    check_line="- [ ] $check_cmd ($(stage_skip_note check); run it before merging)"
  fi
  if stage_ran vignettes; then
    vignette_line="- [x] Every vignette under \`vignettes/articles/\` rendered before the push"
  else
    vignette_line="- [ ] Every vignette under \`vignettes/articles/\` renders ($(stage_skip_note vignettes); run verify_vignettes_parallel.R before merging)"
  fi
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

$regen_text

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

$check_line
$vignette_line
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
}

run_regen() {  # step 7: regenerate the registry artifacts and commit them
  local pre_dirty
  echo
  pre_dirty="$(git status --porcelain)"
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
    {
      echo "ERROR: the registry regeneration failed (its last output is above)."
      echo "  Fix it in $WT_ABS and commit, then run"
      echo "     $(finish_command)"
      echo "  which runs it again, and every later stage still pending, before the push."
    } >&2
    exit 5
  fi

  # Stage whatever the regen actually wrote, not a fixed artifact list: the
  # package renames its derived artifacts (inst/modeldb.qs2 became
  # inst/modeldb.rds), and a fixed list would leave a renamed one unstaged.
  #
  # Every earlier step commits its own work, so the worktree is clean on entry
  # here and anything dirty now is regen output.  Assert that rather than
  # assume it: if the tree was already dirty the operator needs to know, since
  # `git add -A` would sweep unrelated edits into the regen commit, so only
  # the artifact paths that exist are staged.
  if [[ -n "$pre_dirty" ]]; then
    echo "    WARN: worktree was already dirty before the regen step; staging"
    echo "          only known artifact paths to avoid committing unrelated edits:"
    printf '%s\n' "$pre_dirty" | sed 's/^/            /'
    local known=() path
    for path in _pkgdown.yml data inst man NAMESPACE; do
      if [[ -e "$path" ]]; then known+=("$path"); fi
    done
    if (( ${#known[@]} )); then git add -A -- "${known[@]}"; fi
  elif [[ -n "$(git status --porcelain)" ]]; then
    # status, not diff: a regeneration that only creates files changes no
    # tracked one.
    git add -A
  fi
  if ! git diff --staged --quiet; then
    git commit -m "Regenerate modeldb + man docs + pkgdown navbar after merging $SUCCESS branches" >/dev/null
    echo "    committed regen artifacts: $(git show --stat --format= --name-only HEAD | tr '\n' ' ')"
  fi
  stage_set regen "$(git rev-parse HEAD)"
}

run_check() {  # step 8: the devtools::check gate
  echo
  echo "==> Running devtools::check (this can take ~5-15 min)"
  if Rscript -e 'devtools::check(error_on = "error", args = "--no-build-vignettes")' 2>&1 | tail -20; then
    echo "    check passed"
    stage_set check "$(git rev-parse HEAD)"
  else
    {
      echo "ERROR: devtools::check failed. Fix the failures in $WT_ABS and commit, then run"
      echo "     $(finish_command)"
      echo "  which runs check again, and the vignettes if still pending, before the push."
    } >&2
    exit 6
  fi
}

# Step 9, the parallel vignette validation pre-push gate.
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
# rendered, or there were none) plus the per-file results: an Rscript that is
# missing or dies before writing a line -- a failed install, a missing package
# -- leaves no "ok":false to find, so the results alone cannot pass it.
run_vignettes() {  # step 9: every vignette renders
  echo
  echo "==> Parallel vignette validation (every Rmd under vignettes/articles/)"
  echo "    jobs=$VIGNETTE_JOBS  timeout=${VIGNETTE_TIMEOUT}s/vignette"
  # In the worktree's git directory, so they never dirty the tree a later
  # regeneration commits.
  VIGNETTE_RESULTS="$(git rev-parse --path-format=absolute --git-path vignette-results.jsonl)"
  VIGNETTE_LOG="$(git rev-parse --path-format=absolute --git-path vignette-build.log)"
  rm -f "$VIGNETTE_RESULTS"  # never judge this run by an earlier run's results
  local vignette_rc=0
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
    echo "validator's setup) and commit, then run" >&2
    echo "     $(finish_command)" >&2
    echo "which runs the validation again before the push." >&2
    exit 8
  fi
  echo "    all vignettes rendered cleanly"
  stage_set vignettes "$(git rev-parse HEAD)"
}

stage_ran() {  # <stage>: whether it completed and still holds at HEAD
  local state
  state="$(stage_get "$1")"
  [[ "$state" != skip && "$state" != pending ]] && ! stage_stale "$1" "$state"
}

stage_skip_note() {  # <stage>: how a stage that does not hold at HEAD was skipped
  local state
  state="$(stage_get "$1")"
  if [[ "$state" == skip ]]; then
    echo "skipped with $(stage_flag "$1") on the run"
  elif [[ "$state" == pending ]]; then
    echo "skipped with $(stage_flag "$1") on --finish"
  else
    echo "skipped with $(stage_flag "$1") on --finish; the last run, at ${state:0:12}, is out of date"
  fi
}

stage_flag() {  # <stage>: the flag that skips it
  case "$1" in
    regen) echo --skip-r-regen ;;
    check) echo --skip-check ;;
    vignettes) echo --skip-vignettes ;;
  esac
}

stage_stale() {  # <stage> <commit it completed at>: whether the branch has moved under it since
  local stage=$1 done_at=$2 path register
  if [[ "$done_at" == "$(git rev-parse HEAD)" ]]; then return 1; fi
  if [[ "$stage" != vignettes ]]; then return 0; fi
  # The regeneration and check read the registers; the vignettes read the
  # models and the artifacts, so commits that changed only the run's
  # registers leave them standing.
  git rev-parse --verify --quiet "$done_at^{commit}" >/dev/null || return 0
  # To a file, not a process substitution, so a diff that fails stops the run
  # rather than reading as "nothing changed"; --no-renames, so a vignette
  # renamed onto a register path counts as the vignette it was.
  local changed
  changed="$(git rev-parse --path-format=absolute --git-path vignette-diff)"
  git diff -z --no-renames --name-only "$done_at" HEAD > "$changed" \
    || die 5 "cannot tell whether the vignettes are out of date: git diff $done_at HEAD failed."
  while IFS= read -r -d '' path; do
    for register in "$UNION_FILE" "${REGISTER_FILES[@]:-}"; do
      if [[ -n "$register" && "$path" == "$register" ]]; then continue 2; fi
    done
    return 0
  done < "$changed"
  return 1
}

stage_status() {  # <stage>: due, done, run-skip or finish-skip, for --finish now
  local stage=$1 state skipped
  state="$(stage_get "$stage")"
  case "$stage" in
    regen) skipped=$SKIP_R_REGEN ;;
    check) skipped=$SKIP_CHECK ;;
    vignettes) skipped=$SKIP_VIGNETTES ;;
  esac
  if [[ "$state" == skip ]]; then
    echo run-skip
  elif [[ "$state" != pending ]] && ! stage_stale "$stage" "$state"; then
    echo done
  elif (( skipped )); then
    echo finish-skip
  else
    echo due
  fi
}

finish_r_stages() {  # --finish: every R stage the run did not complete, or that is out of date
  local stage state status flags="" due=()
  for stage in regen check vignettes; do
    status="$(stage_status "$stage")" || exit $?
    if [[ "$status" == due ]]; then
      due+=("$stage")
      flags+=" $(stage_flag "$stage")"
    fi
  done
  if (( ${#due[@]} )) && ! command -v Rscript >/dev/null 2>&1; then
    die 3 "Rscript is not on PATH, and these stages are still pending on $BRANCH_NAME: $(join_paths "${due[@]}"). Install R and run --finish again, or add$flags to push without them."
  fi
  # Each stage is judged at its turn: a regeneration that commits makes the
  # stages after it out of date.
  for stage in regen check vignettes; do
    state="$(stage_get "$stage")"
    status="$(stage_status "$stage")" || exit $?
    case "$status" in
      run-skip | finish-skip) echo "    ($stage: $(stage_skip_note "$stage"))" ;;
      done) echo "    ($stage: completed at ${state:0:12})" ;;
      due)
        case "$stage" in
          regen) run_regen ;;
          check) run_check ;;
          vignettes) run_vignettes ;;
        esac ;;
    esac
  done
}


while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) need_value "$@"; REPO="$2"; shift 2 ;;
    --base) need_value "$@"; BASE="$2"; BASE_GIVEN=1; shift 2 ;;
    --pattern) need_value "$@"; PATTERN="$2"; PATTERN_GIVEN=1; shift 2 ;;
    --extra-ref) need_value "$@"; EXTRA_REFS+=("$2"); shift 2 ;;
    --exclude-ref) need_value "$@"; EXCLUDE_REFS+=("$2"); shift 2 ;;
    --branch-name) need_value "$@"; BRANCH_NAME="$2"; shift 2 ;;
    --union-file) need_value "$@"; UNION_FILE="$2"; UNION_FILE_GIVEN=1; shift 2 ;;
    --register-file) need_value "$@"; REGISTER_FILES+=("$2"); REGISTERS_GIVEN=1; shift 2 ;;
    --no-register-files) REGISTER_FILES=(); REGISTERS_GIVEN=1; shift ;;
    --accept-verifier-losses) ACCEPT_VERIFIER_LOSSES=1; shift ;;
    --forbid-path)
      need_value "$@"
      if [[ -z "$2" ]]; then FORBID_PATHS=(); else
        if [[ "${FORBID_PATHS[*]}" == "inst/modeldb.qs2" ]]; then FORBID_PATHS=(); fi
        FORBID_PATHS+=("$2")
      fi; shift 2 ;;
    --skip-r-regen) SKIP_R_REGEN=1; shift ;;
    --skip-check) SKIP_CHECK=1; shift ;;
    --skip-vignettes) SKIP_VIGNETTES=1; shift ;;
    --vignette-jobs) need_value "$@"; VIGNETTE_JOBS="$2"; VIGNETTE_JOBS_GIVEN=1; shift 2 ;;
    --vignette-timeout) need_value "$@"; VIGNETTE_TIMEOUT="$2"; shift 2 ;;
    --skip-push) SKIP_PUSH=1; shift ;;
    --finish) FINISH=1; shift ;;
    --hand-union-done) HAND_UNION_DONE=1; shift ;;
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

if (( HAND_UNION_DONE && ! FINISH )); then
  die 2 "--hand-union-done goes with --finish"
fi

# Each input is one line of the recorded inputs (record_inputs), fields split
# by tabs, so a tab or a newline in one would cut it short there.
for item in "--base=$BASE" "--pattern=$PATTERN" "--union-file=$UNION_FILE" \
  "${REGISTER_FILES[@]/#/--register-file=}" "${EXTRA_REFS[@]/#/--extra-ref=}"; do
  if [[ "$item" == *$'\t'* || "$item" == *$'\n'* ]]; then
    die 2 "${item%%=*} $(printf '%q' "${item#*=}") holds a tab or a newline"
  fi
done

# The same hand-picked refs for every repair and verify step: a step that
# skips them repairs nothing those branches lost.
EXTRA_REF_ARGS=()
for er in "${EXTRA_REFS[@]:-}"; do
  if [[ -n "$er" ]]; then EXTRA_REF_ARGS+=(--extra-ref "$er"); fi
done

if [[ -z "$BRANCH_NAME" ]]; then
  BRANCH_NAME="merge-all-claude-branches-$(date -u +%F)"
fi

cd "$REPO" 2>/dev/null || die 3 "--repo $REPO is not a directory"
# Absolute from here on: the helpers get --repo "$REPO" after the script has
# moved into the worktree, where a relative path would point somewhere else.
REPO="$(pwd)"

# Repo sanity.
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die 3 "$REPO is not a git working tree."

if (( FINISH )); then
  [[ -n "$PYTHON3" ]] || die 3 "python3 is not on PATH; the duplicate check needs it."
  normalise_registers
  WT_ABS="$REPO/.worktrees/${BRANCH_NAME}"
  [[ -d "$WT_ABS" ]] || die 3 "--finish: no worktree at $WT_ABS; run without --finish first."
  cd "$WT_ABS"
  [[ "$(git rev-parse --abbrev-ref HEAD)" == "$BRANCH_NAME" ]] \
    || die 3 "--finish: $WT_ABS is not on branch $BRANCH_NAME."
  DIRTY="$(git status --porcelain)"
  if [[ -n "$DIRTY" ]]; then
    {
      echo "ERROR: --finish: $WT_ABS has uncommitted changes:"
      printf '%s\n' "$DIRTY" | sed 's/^/    /'
      echo "  Commit what belongs on the branch and discard the rest (a step that failed"
      echo "  or was interrupted can leave partial output), then run --finish again."
    } >&2
    exit 3
  fi
  HAND_UNION_REPORT="$(git rev-parse --path-format=absolute --git-path hand-union-report.md)"
  STAGES_FILE="$(git rev-parse --path-format=absolute --git-path merge-stages)"
  INPUTS_FILE="$(git rev-parse --path-format=absolute --git-path merge-inputs)"
  read_inputs
  if [[ "$(stage_get repairs)" == pending ]]; then
    die 3 "--finish: the run that made $WT_ABS stopped before its repairs completed, which --finish does not redo; re-run on a fresh --branch-name."
  fi
  git rev-parse --verify --quiet "$BASE^{commit}" >/dev/null || die 3 "--base '$BASE' does not resolve to a commit"
  echo "==> Finishing $BRANCH_NAME in $WT_ABS"
  if [[ -s "$HAND_UNION_REPORT" ]] && (( ! HAND_UNION_DONE )); then
    {
      cat "$HAND_UNION_REPORT"
      echo
      echo "STOP: the hand-union report above is not empty:"
      echo "    $HAND_UNION_REPORT"
      echo "  Union its wording into the register in $WT_ABS, reconcile any contribution"
      echo "  it lists as missing, and commit, then run"
      echo "     $(finish_command) --hand-union-done"
    } >&2
    exit 9
  fi
  if [[ -s "$HAND_UNION_REPORT" ]]; then
    echo "    hand union confirmed (--hand-union-done); the report stays at $HAND_UNION_REPORT"
  fi
  # The run's gates again, on the branch as the hand edits left it.
  check_no_duplicates
  register_targets
  revert_check
  verify_contributions finish
  if (( ${#VERIFY_LOSS_FILES[@]} )); then
    {
      echo
      echo "STOP: the verifier still reports a model, header or source alias a merged"
      echo "  branch added missing from $(join_paths "${VERIFY_LOSS_FILES[@]}") (listed above)."
      echo "  Reconcile each by hand in $WT_ABS and commit, then run --finish again; to"
      echo "  push with what is left, accept it:"
      echo "     $(finish_command)$( (( HAND_UNION_DONE )) && echo " --hand-union-done") --accept-verifier-losses"
    } >&2
    exit 9
  fi
  SUCCESS=$(git log --first-parent --format=%s "$BASE..HEAD" \
    | grep -c "^Merge branch '.*' into ${BRANCH_NAME}\$" || true)
  finish_r_stages
  push_and_report
  exit 0
fi

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
normalise_registers
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
  pending="$(git -C "$WT_ABS" rev-parse --path-format=absolute --git-path hand-union-report.md 2>/dev/null || true)"
  if [[ -n "$pending" && -s "$pending" ]]; then
    echo "  It holds a hand-union report, which removing it deletes; finish that run" >&2
    echo "  with --finish, or copy the report first:" >&2
    echo "    $pending" >&2
  fi
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
# Kept in the worktree's own git directory, so no step can commit it.
HAND_UNION_REPORT="$(git rev-parse --path-format=absolute --git-path hand-union-report.md)"
STAGES_FILE="$(git rev-parse --path-format=absolute --git-path merge-stages)"
INPUTS_FILE="$(git rev-parse --path-format=absolute --git-path merge-inputs)"
rm -f "$HAND_UNION_REPORT" "$STAGES_FILE" "$INPUTS_FILE"
record_skips
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
    MERGED_REFS+=("$br")
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
        MERGED_REFS+=("$br")
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
record_inputs

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
    "$PYTHON3" "$SCRIPT_DIR/union_merge_lines.py" "${union_args[@]}" --report "$HAND_UNION_REPORT" \
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
  run_dedup "$UNION_FILE" --global
  if ! git diff --quiet -- "$UNION_FILE"; then
    git add "$UNION_FILE"
    git commit -m "Dedup duplicate canonical headers in $UNION_FILE

Two branches that each added the same brand-new ### CANONICAL block
leave exact-duplicate H3 headings after the -X theirs bulk merge.
Collapse each to a single entry, carrying the dropped copies' example
models and source aliases. Wording only a dropped copy had is in the
hand-union report, for a hand union before the push." >/dev/null
    echo "    committed canonical-header dedup"
  else
    echo "    no duplicate canonical headers."
  fi
  check_dedup "$UNION_FILE" --global
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
  run_dedup "$reg"
  if ! git diff --quiet -- "$reg"; then
    git add "$reg"
    git commit -m "Dedup duplicate canonical headers in $reg" >/dev/null
    echo "    committed dedup of $reg"
  fi
  check_dedup "$reg"
done

# Restore whole `### CANONICAL` blocks that -X theirs dropped. The union-merger
# folds Example-model LINES inside buckets that already exist; it cannot bring
# back a canonical whose entire block is gone, which is what happens when a
# branch adds a brand-new canonical and a later branch (based on an older main,
# so lacking it) touches the same region. verify_branch_contributions.sh below
# REPORTS that loss but does not repair it, so this was a manual step on every
# large merge -- 21 blocks on 2026-08-20 alone.
register_targets
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
      --report "$HAND_UNION_REPORT" \
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
    run_dedup "$rf" >/dev/null
    if ! git diff --quiet -- "$rf"; then
      git add "$rf"
      git commit -m "Dedup duplicate canonical headers in $rf" >/dev/null
      echo "    deduped headers in $rf"
    fi
    check_dedup "$rf"
  done
fi

# Verify no per-branch contributions were lost. A loss the verifier reports
# (exit 1) is one no repair could put back -- a canonical two branches filed
# under different ## sections, a brand-new section header the union-merger does
# not relocate (SAPS_II in the 2026-05-20 consolidation) -- and settling it
# needs judgement. Its report goes into the hand-union report, the later steps
# run, and the run stops before the push, where --finish pushes only with
# --accept-verifier-losses; given on the run itself, that flag turns each
# verdict into a WARNING instead. Any other non-zero exit means the verifier
# could not run at all, and carrying on would ship an unverified merge.
verify_contributions run

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
# Every repair has run: --finish can complete the run from here, since it
# runs the gates and the R steps again but none of the repairs.
stage_set repairs "$(git rev-parse HEAD)"

# Revert check. -X theirs resolves a conflicting hunk with the incoming side,
# so a branch cut from an older main can put back its older copy of a whole
# register block: main's newer lines vanish although the branch never touched
# them. None of the repairs above can see that -- they restore what BRANCHES
# added -- and the R regeneration can still pass. On 2026-09-29 a stale copy
# of the fm_<pathway> block dropped nine names from its heading in
# parameter-names.md (16 models then failed the convention tests), and a new
# CONMED_RTV_CC block cut CONMED_RTV_AUC_12H's notes in half. Report every line
# the base has that the merge lost and no merged branch's own diff removed, and
# stop: which copy of a block is right needs judgement, so nothing is repaired.
revert_check

# Steps 7-9, each unless skipped; each records the commit it completed at, so
# --finish runs whichever the run did not complete.
if (( ! SKIP_R_REGEN )); then run_regen; fi
if (( ! SKIP_CHECK )); then run_check; fi
if (( ! SKIP_VIGNETTES )); then run_vignettes; fi

# Push, and print the PR text; stops first when a hand union is pending, which
# includes a loss the verifier reported.
if [[ -s "$HAND_UNION_REPORT" ]]; then
  stop_for_hand_union
fi
push_and_report
