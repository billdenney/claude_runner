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
   the operator can confirm scope before merging. Because this skill
   folds branches in with *real merges* (step 4), a branch from a
   previous consolidation round is a true ancestor of main and so
   reports zero unmerged commits — the survey is authoritative on its
   own, with no content-equivalence guesswork needed to tell whether a
   branch is already in. (Historical note: branches folded in via the
   pre-2026-06 *cherry-pick* flow are NOT ancestors and will still show
   as "unmerged" here even though their content is on main; for a
   one-off transition pass over such branches, fall back to a
   path-based check — "does this branch add a model `.R` file whose
   path is absent on main?".)

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

**ORDER NOTE (changed 2026-08-20).** The register repairs (steps 6, 6b, 6c,
6d) now run BEFORE the R regeneration (step 5 below), not after.
`buildModelDb()` calls `checkModelConventions()`, which treats a duplicate
register entry as an ERROR -- and duplicate entries are exactly what `-X
theirs` produces when two branches each add the same new canonical. Running
the regen first aborted the entire pipeline on damage the very next step
exists to repair. Repair, then regenerate.

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

6b. **Dedup duplicate canonical headers** via
   `dedup_canonical_headers.py --global inst/references/covariate-columns.md`.
   The union-merger folds Example-model *lines* but cannot collapse a
   whole duplicate `### CANONICAL` *block* — which is exactly what
   survives when two branches each ADD the same brand-new canonical
   (both branched from an older main, so `-X theirs` keeps one copy per
   branch). `verify_section_headers.py` only checks headers *survived*,
   not that they are *unique*, so these slip through (8 such pairs were
   on origin/main on 2026-07-25). This step collapses each to one entry
   (unioning example `.R` filenames) and then re-runs with `--check`;
   any duplicate that survives **aborts the run**. `--global` (whole-file
   uniqueness) is correct for the covariate register; the default
   per-`##`-section scope is what you'd use on `compartment-names.md`,
   where the same token is legitimately both a compartment and a suffix.

6c. **Restore whole canonical blocks dropped by the merge** via
   `restore_dropped_sections.py`. The union-merger folds Example-model
   *lines* inside buckets that already exist; it cannot bring back a
   canonical whose ENTIRE `### NAME` block is gone. That happens when a
   branch adds a brand-new canonical and a later branch (cut from an older
   main, so lacking it) touches the same region -- `-X theirs` takes the
   later copy and the block vanishes. `verify_branch_contributions.sh`
   REPORTS this but does not repair it, so it was a manual step on every
   large merge: **21 blocks on 2026-08-20 alone** (AUCMIC_TYLO, HEPARIN_RT,
   CNSREG_PFC/SC, SNP_SLC22A1_RS2282143, STUDY_TLV_PHASE2/3, ...). The
   script preserves each branch's own `##` placement rather than
   re-categorising, and is idempotent.

6d. **Union-merge NEWS.md** via `union_merge_news.py`. NEWS.md has ONE
   append point (`# development version`), so every branch edits the same
   lines and `-X theirs` takes the last branch's whole copy -- which, being
   cut from an older main, is missing what main accumulated since. The loss
   is doubly silent: entries already on main are DELETED *and* every other
   branch's bullet is dropped. On 2026-08-20 NEWS.md came out of the merge
   **85 lines short with not one of the 169 merged models represented**; an
   earlier round lost 60. The script rebuilds from base plus every branch's
   bullets, **gated on the model actually being shipped by this merge** --
   a branch whose tip advanced after the survey may carry bullets for models
   that were not folded in, and announcing those would advertise models the
   package does not have.

7. **Verify no contributions were lost.** Run
   `verify_branch_contributions.sh` which checks, per branch:
   every distinct `*.R` model filename the branch added to
   `inst/references/covariate-columns.md` must appear in the
   reconstructed file. Aborts the pipeline if anything is missing.

   **Known false positive.** The verifier splits a multi-name header such as
   `### CONMED_ATORVASTATIN_DOSE, CONMED_FLV_DOSE, ...` into separate names
   and then cannot find each as a standalone `###` entry, so it reports the
   branch as missing contributions when the block is present. Confirm
   against the file before acting on such a report.

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
    single failure doesn't poison the others.

    **The workers run against a DESCRIPTION-only library path** (default;
    `--full-lib` opts out). The gate links only the packages declared in
    DESCRIPTION's Depends/Imports/Suggests/LinkingTo, plus the render
    harness (rmarkdown/knitr/callr/...), plus the transitive closure of
    both — nothing else on the machine is visible. This exists because a
    vignette that uses an UNDECLARED package renders fine locally (the
    developer happens to have it installed) and then dies on the CI
    runner, which installs only what DESCRIPTION declares. That is not
    hypothetical: pkgdown failed on `Fu_2022_atenolol_qsp` with "there is
    no package called 'units'" *after* this gate had passed all 1215
    vignettes, because `units` was present on the dev box and absent in
    CI. A gate that cannot go red for the thing CI goes red for is not a
    gate. The worker also forces `knitr::opts_chunk$set(error = FALSE)`
    so a chunk error fails the render instead of being written into the
    HTML and reported as success. Continues on failure and
    writes a JSON-lines report (`.vignette_results.jsonl` in the
    worktree). The orchestrator script (`merge_branches.sh`) runs this
    automatically before push; in that script's own step list it is
    step 7 (the `--skip-vignettes` gate).

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

9. **Per-task tracking is automatic.** Because step 4 used real
   merges, each source `claude/<task-id>` branch tip is already an
   ancestor of the consolidation branch. Once the consolidation PR
   lands on main, `git branch --merged origin/main` lists every
   folded branch and GitHub marks each as "Merged" — no force-advance
   step, no `post_merge_advance.sh`, no SHA bookkeeping. The operator
   can then delete the source branches at leisure
   (`git push --delete origin claude/<task-id>`).

10. **Push the branch** to origin:

    ```bash
    git push -u origin <branch-name>
    ```

11. **Print the suggested PR title + body** for the operator to open
    manually. The body lists which branches were folded in,
    categorised as new-model additions / vignette ASCII fixes /
    follow-up edits, plus a procedural note on the `-X theirs`
    caveat for covariate-columns.md.

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

### Per-task tracking: real merges make it free

The skill folds each source branch in with a real
`git merge --no-ff -X theirs` (one merge commit per branch), NOT
cherry-pick. This is the design decision that makes per-task tracking
*free*: a real merge makes each source branch's tip a true ANCESTOR
of the consolidation branch, so the moment the consolidation PR lands
on main, every folded branch is reported as merged by
`git branch --merged origin/main` and shown as "Merged" on GitHub.
There is no SHA rewrite, no `post_merge_advance.sh`, and — crucially —
no content-equivalence guesswork to decide whether a branch is already
in. "Is this branch merged?" is a one-line ancestor query.

This replaces the older cherry-pick flow, which gave every folded
branch a NEW commit SHA. Cherry-picked branches never became
ancestors of main, so GitHub showed them as "1 commit ahead"
indefinitely and an extra `post_merge_advance.sh` force-advance step
(plus per-branch SHA bookkeeping) was needed to fake the ancestry.
That whole apparatus is gone.

The historical objection to merge — "merging a stale-based branch
rolls back main-side updates" — does not survive scrutiny: the only
files a stale branch can roll back are the shared bookkeeping files,
and every one of those is rebuilt or repaired downstream (registry
blobs + `man/` + navbar regenerated in step 5; covariate-columns.md
union-merged in step 6). New model `.R` / vignette `.Rmd` files live
at unique paths, so a 3-way merge keeps every prior branch's
additions intact. `-X theirs` only changes how *conflicting* hunks
resolve, which is incidental for exactly those regenerated /
union-merged files.

One-off transition caveat: branches that were folded in by the OLD
cherry-pick flow are still not ancestors of main, so a consolidation
pass that needs to re-examine them cannot rely on the ancestor check
alone — use a path-based content check ("does the branch add a model
`.R` at a path absent on main?") for that single transition pass.
Every branch merged by *this* (merge-based) skill is trackable by
ancestry from then on.

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
