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
