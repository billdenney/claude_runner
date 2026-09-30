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
    --exclude-ref origin/claude/<wip-task-branch> \
    --branch-name "merge-all-claude-branches-$(date +%F)"
```

That single command runs the entire end-to-end pipeline. The survey aborts
(exit 4) when two branches add a file at the same path with different content,
because `-X theirs` would silently keep only the last one; reletter one branch's
files (`<Author>_<Year>a_` / `<Year>b_`) or exclude it. `--exclude-ref`
(repeatable) leaves out a branch the pattern matches -- a WIP checkpoint, or a
branch that already has its own PR -- and prints each exclusion in the survey. The flags
have sensible defaults for the nlmixr2lib popPK ingestion use case;
override per repo. `--extra-ref` (repeatable) adds a hand-picked branch the
pattern does not match; it reaches every repair and verify step. An excluded
branch reaches none of them: every helper reads only the merge set (see
"The merge set" below).

From an agent's shell, which has no terminal, pass `--yes` once the operator
has confirmed the scope (step 2): without it the script stops at its
confirmation prompt with exit 3 and creates nothing. `merge_branches.sh --help`
prints every flag and exit code. Each exit code has one meaning; the ones that
stop a run partway are 5 (a repair or verification step failed, the verifier
could not run, or the revert check found base content the merge reverted),
6 (`devtools::check`), 7 (push) and 8 (vignettes).

## The merge set

`--pattern` matches more than the branches a run merges: branches left out with
`--exclude-ref`, branches pushed after the survey (or pushed to again after
they were merged), and branches from earlier rounds that the base already has.
On 2026-09-29 the helpers still read the 18 branches that run had left out:
6 orphan `**Example models:**` entries and 6 NEWS bullets for models the merge
did not ship leaked in, and the verifiers reported dozens of their
contributions as missing. (Two more orphan entries came from branches that were
merged, from content they had only inherited; see step 6.)

So every repair and verify helper asks `merge_set.py` which branches count,
and what each contributed. For a branch R, consolidation branch B and base:

- **merged commit** `M = git merge-base R B`: R's tip when R is an ancestor of
  B. If R's tip moved on after B merged it, M is the part B merged, and the
  later commits count for nothing.
- R is **in the merge set** when M is not an ancestor of the base, i.e. B
  merged something of R's that the base lacks. A branch never merged has M at
  its fork point, which the base has; a branch from an earlier round is on the
  base. A branch merged only through another merged branch (one an
  `--extra-ref` branch had merged into itself, say) is in it too: its commits
  are in B.
- R's **own diff** runs from its fork point `F = git merge-base <base> M` to M.
  What R has at F it inherited from main; main may have renamed or removed it
  since, so it is not R's contribution.

Each helper prints what the gate left out, and names each branch that moved
on; a branch already on the base is not mentioned. A helper whose merge set is
empty exits 2: the branch or the pattern is wrong, and a check of nothing must
not pass. `python3 merge_set.py --repo ... --branch ...` prints the members,
one `<ref> <merged commit> <fork point>` line each.

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
6d) now run BEFORE the R regeneration (step 5 below), not after, and so do the
verifier (step 7) and the revert check (step 7b).
`buildModelDb()` calls `checkModelConventions()`, which treats a duplicate
register entry as an ERROR -- and duplicate entries are exactly what `-X
theirs` produces when two branches each add the same new canonical. Running
the regen first aborted the entire pipeline on damage the very next step
exists to repair. Repair, then regenerate.

5. **Regenerate registry artifacts** via R:

   ```bash
   Rscript -e 'devtools::load_all("."); nlmixr2lib:::buildModelDb(); devtools::document()'
   ```

   - `buildModelDb()` writes the registry blob (`data/modeldb.rda` plus
     whichever of `inst/modeldb.rds` / `inst/modeldb.qs2` the package
     currently ships) and refreshes the pkgdown navbar.
   - `document()` regenerates `man/*.Rd`.

   **Do not hardcode the artifact list when staging.** The script stages
   whatever the regen actually wrote (`git add -A`, valid because every
   earlier step commits its own work, and guarded by a pre-regen dirty
   check). The old hardcoded list named `inst/modeldb.qs2` — a path on this
   script's own forbidden-resurrection list, because nlmixr2lib had already
   moved to `inst/modeldb.rds`. It therefore staged a file that could not
   exist and never staged the one that did, which would have pushed a
   registry blob stale against 181 new model files.

6. **Union-merge covariate-columns.md** via
   `union_merge_lines.py`. This script:
   - Reads each branch in the merge set whose own diff touched the file, at
     its merged commit.
   - Takes from each branch only the `**Example models:**` entries it added,
     or whose annotation it changed, relative to its fork point. An entry it
     merely inherited is main's, and main may have renamed or removed it
     since: on 2026-09-29 the pre-rename `Willmann_2018_rivaroxaban.R` came
     back twice that way, from branches that had been merged.
   - Buckets the entries by (covariate header, subsection) and unions them
     with the base's, keeping the most informative (longest) annotation per
     filename.
   - Rebuilds each Example-models line the union adds to: the line's own
     models first, then the branches' additions. The line keeps its prefix
     and the text after its last entry.

   **The emitter is idempotent (fixed 2026-09-29).** It used to rebuild every
   line as `", ".join(entries) + "."`. Where an annotation opens a `(` it never
   closes, the parser ran to the end of the line, so the annotation took the
   line's final full stop and the emitter appended another: one per round,
   until three lines of nlmixr2lib's covariate register (in the
   `BACT_PTT_LOG10CFU`, `CONMED_QPRL_ORAL` and `FORM_VINP_IR` blocks) ended in
   13 or more. It also turned `; ` separators into `, ` and dropped prose
   between or after the entries. Now an unclosed annotation ends before the
   next entry, else after its last `)`, else before the trailing full stops;
   the text after the last entry is kept as it is; and a line the union adds
   nothing to is left byte for byte, so running the union twice gives what
   running it once gives. A line it does add to is still rebuilt with `, `
   between entries, and text between two entries is not kept. Existing runs
   of full stops are left as they are; they no longer grow. An Example-models
   line with no inline list (a list-style heading with one sub-bullet per
   model, or prose) is never rewritten; the placement check (step 7) reports
   a branch's model missing from it.

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
   `restore_dropped_sections.py`. This runs against `--union-file` AND every
   `--register-file` (default `compartment-names.md`, `parameter-names.md`),
   which are deduped with per-`##`-section scope rather than `--global`.
   Restricting the repairs to the union file alone lost 11 canonical blocks
   on 2026-08-31 across two registers that 23 and 11 branches had touched. The union-merger folds Example-model
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

   **It is gated on the merge set (added 2026-09-13).** Without that gate it
   resurrected blocks that had been removed ON PURPOSE, which is worse than the
   loss it repairs -- on an 80-branch round it proposed 31 blocks of which 20
   were pre-rename spellings. Three skips, each reported in its output:

   - **the merge set** -- only branches in the merge set (see "The merge set"
     above) contribute, each read at its merged commit; the `--pattern` glob
     also matches earlier rounds, branches left out with `--exclude-ref` and
     branches pushed after the survey. A branch that moved on after it was
     merged contributes the part that was merged. (The ancestry test this
     replaced skipped such a branch outright, so a block it contributed and
     the merge lost was not restored.)
   - **fork point** -- only blocks a branch ADDED count. Comparing against the
     current base is not enough: when main renames a canonical, every branch cut
     before the rename still carries the old spelling, which is then absent from
     both the base and the merge result. Comparing against the branch's own
     `merge-base` shows it was inherited.
   - **deliberate removal** -- a name removed by a NON-MERGE commit on the
     branch was retired on purpose (e.g. applying an operator naming ruling
     that post-dates the branch); restoring it would undo the rename on every
     re-run. Tested by comparing PARSED BLOCK SETS across each non-merge commit
     (present in the parent, absent in the commit), which is immune to
     `union_merge_lines.py` rewriting the file and moving every header. Note
     that a name can ALSO vanish at a later MERGE -- that is the real loss this
     script repairs, so only non-merge commits count. Both simpler tests fail:
     scanning diffs for removed `### ` lines mislabelled 526 blocks, and
     "present at the last merge commit" breaks as soon as a reconciled branch
     has more branches folded into it, which is how a consolidation grows.

   It also indexes EVERY name in a multi-name header (`### fm_a, fm_b, fm_c`).
   When a folded branch added a name to a header that survived under a different
   name, the block is present but the name is not; re-inserting the block would
   duplicate it, so that case is REPORTED for a manual header union rather than
   auto-repaired. `buildModelDb()` fails on those until fixed -- that is how the
   loss of `fm_cysmer` / `fm_gluc` / `fm_sulf` surfaced when no register
   verifier caught it.

   Regression tests: `tests/unit/test_restore_dropped_sections_gate.py`.

6d. **Union-merge NEWS.md** via `union_merge_news.py`. NEWS.md has ONE
   append point (`# development version`), so every branch edits the same
   lines and `-X theirs` takes the last branch's whole copy -- which, being
   cut from an older main, is missing what main accumulated since. The loss
   is doubly silent: entries already on main are DELETED *and* every other
   branch's bullet is dropped. On 2026-08-20 NEWS.md came out of the merge
   **85 lines short with not one of the 169 merged models represented**; an
   earlier round lost 60. The script rebuilds from base plus **the bullets
   each branch in the merge set added**: present in NEWS.md at its merged
   commit, absent at its fork point. A branch left out of the merge adds
   nothing, and one that moved on after it was merged adds only what the
   merged part added.

   This provenance gate replaced one that parsed "Add <Author> <Year>" and
   kept a bullet when a shipped model file had that author and year. On
   2026-09-29 that was wrong both ways: it kept six bullets from branches
   left out of the merge, because another shipped model shared their author
   and year ("Add Wang 2020 caspofungin" rode in on a shipped Wang 2020
   model), and it dropped bullets whose file stem spells them differently: a
   lettered year (`Chen_2021a_tacrolimus.R`) or a surname particle ("Le
   Marouille", `Marouille_2021_palbociclib.R`). Reading each branch's whole
   file also brought back three bullets main had reworded since those
   branches forked, in their old wording, beside the new. The rebuild now
   lists any bullet of the merge result it drops.

7. **Verify no contributions were lost.** Run
   `verify_branch_contributions.sh`, which now applies THREE checks per
   branch, per register file:

   a. **Filename** — every distinct `*.R` the branch added must appear
      somewhere in the reconstructed file.
   b. **Section header** (`verify_section_headers.py`) — every brand-new
      `##` / `### CANONICAL` header the branch introduced must survive.
   c. **Placement** (`verify_register_placement.py`) — every
      *(canonical, model.R)* pair the branch recorded must still be filed
      **under that canonical**.

   Check (c) was added 2026-08-31 because (a) and (b) both pass in the case
   that matters most: two branches register the SAME canonical from mains
   lacking each other's copy, `-X theirs` keeps one entry, and the other's
   aliases and example models are discarded. The surviving entry keeps the
   header (so (b) passes) and the dropped models are usually cited elsewhere
   in the file (so (a) passes). Four such losses survived every check on the
   97-branch consolidation: `UGT2B15_STAR2_HET`/`_HOM`,
   `RRT_CRRT_EFFLUENT_FLOW`, and `lkst`.

   Two subtleties, both learned the hard way:

   - All three checks read only **the merge set**, and only **what each
     branch added**: its own diff, from its fork point to its merged commit.
     The queue keeps pushing while a merge runs and `--exclude-ref` leaves
     branches out, so the pattern also matches branches that are not in the
     merge; on 2026-09-29 the unmerged branches produced dozens of false
     "missing" reports. Content a branch merely inherited is main's: if the
     merge loses it, step 7b reports it against the base.
   - A header may name several canonicals sharing one block
     (`### QTc, QTcF, QTcI, QTcP, QTcS`), and that list GROWS as spellings are
     ratified. Checks (b) and (c) index each name separately, so a block that
     *gained* a name is not read as a different canonical with everything
     under it lost.

   The verifier exits 1 when it finds losses, and `merge_branches.sh` turns
   that into a WARNING to reconcile by hand. It exits 2 when it cannot run at
   all: a `--base`, `--branch` or `--extra-ref` that does not resolve, no
   worktree for the branch, a `--pattern` matching nothing, an empty merge
   set, or no `python3`. Each of those used to pass silently -- a verifier
   that checks nothing reports everything present -- and `merge_branches.sh`
   now stops with exit 5 on them. The helpers it calls (`union_merge_lines.py`,
   `restore_dropped_sections.py`, `union_merge_news.py`, the two delegated
   verifiers, `verify_no_base_reverts.py`, `merge_set.py` and
   `dedup_canonical_headers.py`) apply the same rule: a missing worktree, file
   argument or matching branch, or an empty merge set, is an error, never a
   skip.

   **A report against a multi-name heading is real.** This section used to
   call such a report (`### fm_125d3, fm_1ohm, ...`) a known false positive,
   to be confirmed against the file and set aside. On 2026-09-29 the `fm_*`
   placement reports were real: a branch cut before main added nine pathways
   to that heading won the merge, and the heading lost them, with main's
   example entries, a paragraph and the wording of its notes.
   `buildModelDb()` passed anyway, which proved nothing; the package's
   convention tests then failed on 16 models. Checks (b) and (c) compare
   heading names one by one, so a heading whose list merely grew is not
   reported; content of the base lost this way is reported by step 7b, which
   stops the run. Treat any report as real until the file shows otherwise.

7b. **Revert check** via `verify_no_base_reverts.py`, on the union file and
   every register file, after the NEWS union and before the regeneration.
   `-X theirs` resolves a conflicting hunk with the incoming side, so a
   branch cut from an older main can put back its older copy of a whole
   block: main's newer lines vanish although the branch never touched them.
   The repairs above cannot see this -- they restore what BRANCHES added --
   and the regeneration can pass. On 2026-09-29 a stale copy of the
   `fm_<pathway>` block in parameter-names.md lost the nine names above, and
   a new `### CONMED_RTV_CC` block pasted into the middle of
   `CONMED_RTV_AUC_12H`'s notes line cut it in two, taking its second half
   into the new block.

   It reports every piece of the base's content that the merge result lacks
   and that no merge-set branch's own diff removed; a line a branch edited or
   deleted on purpose is therefore not reported. Content is compared block by
   block: a `### ` block is matched, within its own `## ` section, to the
   result's blocks that share a heading name with it (a multi-name heading is
   split on commas, and its `(**...**)` description is not a name). So a
   block that moved is not a loss, and a token two sections both use
   (compartment-names.md registers some as a compartment and as a suffix) is
   never matched across them. Each heading name, the heading's description,
   each Example-models entry and its annotation, and every other non-blank
   line must survive; how many full stops end an Example-models line is not
   compared. A line cut in two by a pasted block -- its first part kept, the
   rest now ending a line of another block -- is reported even though the
   branch's own commit did it, which is what happened to CONMED_RTV_AUC_12H.

   Exit 0 is clean, 1 lists every affected block and line, 2 means it could
   not run. `merge_branches.sh` stops with exit 5 on either of the last two,
   before the regeneration. Nothing is repaired automatically, because which
   copy of a block is right needs judgement: put the base's content back by
   hand, keeping what the branches added, commit, and re-run the check until
   it passes. Replayed on the 2026-09-29 merge after the automated repairs,
   it reported those two blocks and nothing else; the hand-repaired branch
   passes it.

   Regression tests: `tests/unit/test_verify_no_base_reverts.py`.

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
    code 8 on any failure, including a validator that dies before
    recording a result, e.g. on a failed install):

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
    step 9 (the `--skip-vignettes` gate).

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
   (`git push --delete origin claude/<task-id>`). The same ancestry makes
   each completed task's local worktree reclaimable: once the PR lands,
   `claude-task-runner worktree reclaim --queue <queue>` lists them (a dry
   run) and `--apply` removes them with their local branches (ADR-0034).
   The supervisor does this on its own when `[worktree_reclaim].periodic`
   is on.

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
- **Does not remove the per-task worktrees.** That is
  `claude-task-runner worktree reclaim`'s job, after the consolidation PR
  has landed (see step 9).
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
which branches would be folded in before committing to the merge,
then run again with `--yes` once the operator agrees.

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
