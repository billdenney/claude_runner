---
name: runner-merge-claude-branches
description: |
  Use this skill when the user wants to consolidate the many
  per-task ``claude/<task-id>`` branches the runner has pushed into
  one review-ready branch + PR. Triggers:
  "/runner-merge-claude-branches", "merge claude branches",
  "consolidate the task branches", "fold all the model-extraction
  branches into one PR", "build a mega-PR from the runner output".

  A runner queue that has been live for any length of time accumulates
  dozens of pushed but unmerged ``claude/<task-id>`` branches — one
  per dispatched task that committed real work. Opening one PR per
  branch is impractical at that scale; the operator wants one bulk
  PR. This skill orchestrates the merge end-to-end and surfaces
  the right manual steps for the cases the script can't safely
  automate.

  Outputs:
    * a new worktree on a fresh branch off ``origin/main``
    * all eligible ``claude/*`` branches merged in
    * registry artifacts regenerated via R (data/modeldb.rda,
      inst/modeldb.qs2, man/*.Rd, _pkgdown.yml navbar)
    * inst/references/covariate-columns.md union-merged so per-
      branch annotations are preserved (the structured-markdown
      file that ``-X theirs`` clobbers)
    * branch pushed to origin
    * suggested PR title + body printed for the operator to open
      manually via the GitHub web UI (the user's ``gh`` CLI is
      read-only by policy)
---
# /runner-merge-claude-branches — consolidate task branches into one PR

This skill is *partially* automated. The merge mechanics and the
post-merge regenerations are scripted; the operator-facing
decisions (which branches to include, whether `devtools::check`
passes, whether to open the PR) stay interactive.

## Quick start

```bash
bash /home/bill/.claude/skills/runner-merge-claude-branches/merge_branches.sh \
    --repo /home/bill/github/nlmixr2/nlmixr2lib \
    --base origin/main \
    --pattern 'origin/claude/*' \
    --branch-name "merge-all-claude-branches-$(date +%F)"
```

That single command runs the entire end-to-end pipeline. The flags
have sensible defaults for the nlmixr2lib popPK ingestion use case;
override per repo.

## Steps the skill follows

1. **Pre-flight survey.** Identify which `origin/claude/*` branches
   have unmerged commits (i.e. `git rev-list --count origin/main..<br>`
   is non-zero). Print the list, file counts, and commit subjects so
   the operator can confirm scope before merging.

2. **Operator confirms scope.** Present the list via
   `AskUserQuestion` with options for: all branches, the new-model
   ones only, or a manual pick. (For now `merge_branches.sh` accepts
   `--pattern` for filtering; richer interactive scoping can be added.)

3. **Create the worktree** at `<repo>/.worktrees/<branch-name>` off
   the configured base (default `origin/main`). The new branch
   tracks the base; no commits yet.

4. **Sequential merge.** For each candidate branch, run
   `git merge --no-ff --no-edit -X theirs -m "Merge branch '<br>' into <new-branch>" <br>`.

   - `-X theirs` resolves binary registry conflicts (data/modeldb.rda,
     inst/modeldb.qs2) and overlapping text additions on shared
     metadata files (_pkgdown.yml, NEWS.md) in favour of the
     incoming side. Those files get regenerated authoritatively
     in step 5, so the choice during merge is incidental.

   - **WARNING — covariate-columns.md.** This file's structured
     `**Example models:**` lines get clobbered by `-X theirs` when
     multiple branches each rewrite the same line to add their own
     model. Step 6 below repairs the damage via a union merger;
     do NOT skip that step.

5. **Regenerate registry artifacts** via R:

   ```bash
   Rscript -e 'devtools::load_all("."); nlmixr2lib:::buildModelDb(); devtools::document()'
   ```

   - `buildModelDb()` writes `data/modeldb.rda` + `inst/modeldb.qs2`
     and refreshes the pkgdown navbar.
   - `document()` regenerates `man/*.Rd`.

6. **Union-merge covariate-columns.md** via
   `union_merge_lines.py`. This script:
   - Parses each branch's diff for added `**Example models:**` lines.
   - Buckets additions by (covariate header, subsection).
   - Builds a union of `(filename, annotation)` per bucket,
     preserving the most-informative annotation per filename.
   - Re-emits each Example-models line as a single deduplicated list.

7. **Verify no contributions were lost.** Run
   `verify_branch_contributions.sh` which checks, per branch:
   every distinct `*.R` model filename the branch added to
   `inst/references/covariate-columns.md` must appear in the
   reconstructed file. Aborts the pipeline if anything is missing.

8. **`devtools::check` pre-push gate**:

   ```bash
   Rscript -e 'devtools::check(error_on = "error", args = "--no-build-vignettes")'
   ```

   The `--no-build-vignettes` works around the known-pre-existing
   CarlssonPetri segfault on this codebase. Expect `0 errors / 0
   warnings / 1 note` (the `.git`-in-worktree note is pre-existing
   and ignorable).

   **Important**: this gate does NOT cover vignette evaluation. Step
   8b below does that — do not skip it.

8b. **Parallel vignette validation pre-push gate** (HARD gate; exit
    code 8 on any failure):

    ```bash
    Rscript verify_vignettes_parallel.R \
      --worktree <worktree> \
      --jobs $(($(nproc) - 2)) \
      --timeout 900
    ```

    Renders every `vignettes/articles/*.Rmd` in a callr subprocess so a
    single failure doesn't poison the others. Continues on failure and
    writes a JSON-lines report (`.vignette_results.jsonl` in the
    worktree). The orchestrator script (`merge_branches.sh`) runs this
    automatically before push.

    **Why this gate exists.** pkgdown's CI vignette build runs
    sequentially and ABORTS on the first failure. After a 130-branch
    merge that can leave 14+ latent failures undiscovered, each
    surfaced one at a time across many CI iterations. Catching them
    all in one local parallel pass keeps the PR loop short and
    surfaces shared root causes (e.g. the "rxUi auto-injects `cmt()`
    for algebraic observables AFTER ODE states and renumbers slots"
    bug pattern that broke 12 vignettes in the 2026-06-17 merge) in
    one batch.

    **What to do on failure.** The `.vignette_results.jsonl` lists
    every failing vignette with its error message. Common patterns:

    - `chol(): decomposition failed` — model has a rank-1 / numerically
      indefinite OMEGA matrix. Re-encode as a single standardized
      shared eta scaled per-parameter in `model({})` instead of a
      multi-eta block with `r = +1`. See
      `inst/modeldb/specificDrugs/Fanta_2007_ciclosporin.R` for the
      canonical example.
    - `'cmt' on observation record or on a undefined compartment` /
      `following parameter(s) are required for solving: <state>` /
      vignette filters dropping all rows — the model declares ODE
      states (`d/dt(central) <- ...`) plus algebraic observables
      (`Cc <- central / vc`) and the vignette event table references
      the observables on observation rows (`cmt = "Cc"`). rxUi
      auto-injects `cmt()` for the observables AFTER the ODE states,
      renumbering slot indices and breaking references to ODE states
      past the inserted slot. **Fix in the EVENT TABLE**: change the
      observation `cmt` value to the actual ODE state name (e.g.
      `cmt = "central"`). rxode2 returns every algebraic observable
      as a column in the output regardless of which compartment the
      `cmt` pointed at — the `cmt` says when, not what. **Do NOT
      add `cmt()` declarations to `model({})`** to silence this; that
      pollutes the model body to mask a bug whose home is in the
      event table.
    - `unique(x) returned >1 value` in dplyr `summarise()` — the
      grouping is too coarse. Fix the `group_by` to include the
      covariate that varies, or switch to `first()` / `mean()`.
    - `callr timed out` — the vignette ran longer than the 900s
      ceiling. Usually a too-large `n_per_group` for the merge's
      parallel-worker contention; reduce the cohort size or raise
      `--vignette-timeout` if the run legitimately needs it.

    Skipping this gate (`--skip-vignettes`) is allowed only for
    iteration. NEVER push without it green on the final pass.

9. **Emit `post_merge_advance.sh`** in the worktree root. This is a
   small, idempotent script the operator runs AFTER the
   consolidation PR is merged. It force-advances each source
   `claude/<task-id>` branch's tip to its cherry-picked commit on
   main, so per-task tracking is preserved — GitHub then shows each
   source branch as "merged" instead of the perpetual "1 commit
   ahead" that cherry-pick's SHA rewrite causes. Safety: uses
   `--force-with-lease=<branch>:<original_sha>` so a source branch
   that's been updated since cherry-pick (e.g. the runner
   re-dispatched the task) is not silently clobbered.

10. **Push the branch** to origin:

    ```bash
    git push -u origin <branch-name>
    ```

11. **Print the suggested PR title + body** for the operator to open
    manually. The body lists which branches were folded in,
    categorised as new-model additions / vignette ASCII fixes /
    follow-up edits, plus a procedural note on the `-X theirs`
    caveat for covariate-columns.md AND instructions for running
    `post_merge_advance.sh` after the PR merges.

## Things this skill does NOT do

- **Does not open the PR.** Per the user's global instructions the
  `gh` CLI is read-only. The skill prints the title + body and the
  URL the operator can paste.
- **Does not merge into main.** Always pushes the new branch and
  leaves opening / merging the PR to the operator.
- **Does not delete the source `claude/*` branches.** Those stay on
  origin as the per-task audit trail. When the consolidation PR
  merges, the operator can clean up via `git push --delete origin
  claude/<task-id>` as they prefer.
- **Does not modify the queue's runtime state.** The supervisor /
  daemons / sidecar files are untouched. The merge runs entirely
  inside the nlmixr2lib repo's worktree.

## Important nuances

### Vignette failures are EXPECTED on major merges

Every consolidation of this size has historically surfaced a handful
of broken vignettes that the per-paper extractions did not catch.
The 2026-06-17 merge surfaced 15. Common shapes:

- A model whose IIV block was published with `r = +1` between several
  etas (rank-1 OMEGA) — fine for fitting, but rxode2's Cholesky-
  based simulator can't decompose it. Re-encode as a single
  standardized shared eta.
- Vignettes whose event tables reference algebraic observables
  (e.g. `cmt = "Cc"`) instead of ODE state names. Auto-injected
  `cmt()`s shift slot numbering and break references to ODE states
  and dose history. Fix: in the event table, use the actual ODE
  state name (`cmt = "central"`) on observation rows; rxode2
  reports the algebraic observable in the output dataframe
  regardless. Do NOT add `cmt()` calls to `model({})` — that
  pollutes the model body to silence the symptom.
- Per-vignette code bugs (`unique(x)` on a varying column;
  `filter()` chains that drop every row; cohort sizes that overflow
  the per-vignette timeout under parallel-build contention).

**This is a recurring failure mode**, not a one-off. The validation
gate in step 8b exists specifically to catch all of them in one
local parallel pass instead of dribbling them through the CI
sequential build one at a time. NEVER push a consolidation branch
without the green gate.

### Per-task tracking: cherry-pick + post-process

The skill uses `git cherry-pick -X theirs` rather than `git merge -X
theirs` because the latter rolls back earlier-merged branches' work
when source branches are based on stale main (the common case for
long-lived `claude/*` task branches). Cherry-pick applies only the
commit delta, but each cherry-picked commit gets a NEW SHA — so
source branches never become true ancestors of main, and GitHub
shows them as "1 commit ahead" indefinitely after the consolidation
PR merges.

`post_merge_advance.sh` resolves this. Step 9 emits the script
inside the worktree, recording a mapping of
`(source_branch, cherry_picked_sha, original_source_sha)` per
cherry-picked branch. After the consolidation PR merges into main:

```bash
cd <repo>/.worktrees/<branch-name>
bash post_merge_advance.sh             # dry-run; shows what would happen
bash post_merge_advance.sh --apply     # force-push each source branch
```

The `--force-with-lease=<branch>:<original_sha>` form means a source
branch that was UPDATED since cherry-pick time (e.g. the runner
re-dispatched the same task and pushed new commits) will be
rejected rather than silently clobbered. Operator then triages
those individually.

If the operator forgets to run the advance script, nothing breaks —
the source branches just stay "1 commit ahead" and can be deleted
manually if desired. The script is optional and idempotent.

### Why `-X theirs` for binaries is safe

Every model-addition branch touches `data/modeldb.rda`,
`inst/modeldb.qs2`, `man/modeldb.Rd`, and (often) `_pkgdown.yml`.
These are derived artifacts: `buildModelDb()` regenerates them
deterministically from the union of all model `.R` files now on
the branch. So the merge-time choice for the binary blob doesn't
matter — step 5 overwrites it canonically.

### Why `-X theirs` for covariate-columns.md is NOT safe

This file's `**Example models:**` lines are structured per-covariate
listings where each branch *appends* its model to the existing
list. Branches don't add new sections; they edit the same line.
With `-X theirs`, only the last branch's version survives, and
every other branch's annotation gets lost. The union merger in
step 6 reconstructs this by parsing every branch's diff and
emitting a deduplicated union.

If you're operating on a different codebase that has its own
structured-markdown collation file (e.g. an `AUTHORS.md` where each
branch adds a name), pass `--union-file <path>` to
`merge_branches.sh` to point the union merger at it.

### `--dry-run` mode

`merge_branches.sh --dry-run` runs steps 1-2 (survey + confirm
scope) but stops before creating the worktree. Use this to see
which branches would be folded in before committing to the merge.

### Idempotence

The script aborts cleanly if the target worktree already exists.
To re-run, remove the worktree first:

```bash
git -C <repo> worktree remove --force .worktrees/<branch-name>
git -C <repo> branch -D <branch-name>
```

### What to do if `devtools::check` fails

The script does NOT auto-fix check errors. If step 8 reports any
errors or new warnings (beyond the pre-existing `.git` NOTE):
1. The worktree is left in place with all merges intact.
2. The operator can re-run check, investigate, and either fix the
   issue or revert specific merges.
3. The push step (9) does NOT run if check fails — the operator
   confirms before pushing.

### When new claude/* branches appear during the merge

Unlikely but possible: a long-running queue could push a new
branch while this skill is running. The pre-flight survey
captures the initial set; new branches are not auto-included
during execution. Re-run the skill for a fresh consolidation
round if needed.
