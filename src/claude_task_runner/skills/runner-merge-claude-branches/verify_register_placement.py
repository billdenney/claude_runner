#!/usr/bin/env python3
"""Verify per-CANONICAL placement of model contributions after a bulk merge.

The pre-existing filename check in verify_branch_contributions.sh asks whether
each `*.R` a branch added appears ANYWHERE in the register file.  That is not
the contract the register actually encodes: a model listed under the wrong
canonical -- or under no canonical at all, because a competing branch's entry
won the `-X theirs` resolution -- still satisfies a file-wide grep.

This check asks the stronger question: for every (canonical, model.R) pair a
branch recorded, is that model still listed UNDER THAT CANONICAL after the
merge?

A branch "recorded" a pair when its own diff added it: present at the commit
the consolidation merged, absent at that commit's fork point from the base.
Only branches in the merge set count (merge_set.py). Pairs a branch merely
inherited are main's content, not the branch's: when a stale copy of a block
wins the merge and loses them, verify_no_base_reverts.py reports that loss
against the base, and a pair main has since removed or renamed (a pre-rename
model file, say) is not expected in the merge at all.

It exists because a 97-branch consolidation (2026-08-31) lost four such pairs
while the filename check passed on all of them:

  UGT2B15_STAR2_HET / _HOM   Stringer 2014's entry survived; Stringer 2013's
                             aliases and models were dropped, but its models
                             were cited elsewhere in the file.
  RRT_CRRT_EFFLUENT_FLOW     ButraguenoLaiseca 2024 survived; 2022 was dropped.
  lkst                       two branches registered it independently; the
                             later one won and took the earlier ratification's
                             example models with it.

That is the "two branches register the same canonical" case, which neither the
union-merger (it rebuilds only Example-models LINES that already share a
bucket) nor restore_dropped_sections.py (it restores only blocks that vanished
ENTIRELY) repairs.

Exit codes: 0 clean (or the merged file is not in the worktree), 1 missing
placements found, 2 the check could not run: a bad argument, a --base, --branch
or --extra-ref that does not resolve, no worktree for --branch, no branch
matching --pattern, an empty merge set, or a crash.
"""

import argparse
import collections
import re
import sys
import traceback
from pathlib import Path

import merge_set

R_FILE = re.compile(r"`([A-Za-z0-9_.\-]+\.R)`")


def fail(message: str) -> int:
    print(f"ERROR: (placement) {message}", file=sys.stderr)
    return 2


def parse(text: str) -> dict[str, set[str]]:
    """canonical -> set of model .R filenames listed under it.

    Only Example-models lines and source-alias bullets count as a *listing*.
    Prose in Notes may legitimately mention a model without filing it there.
    """
    out: dict[str, set[str]] = collections.defaultdict(set)
    names = []
    for ln in text.splitlines():
        if ln.startswith("### "):
            # A header may legitimately name several canonicals that share one
            # block ("### QTc, QTcF, QTcI, QTcP, QTcS"), and that list GROWS as
            # new spellings are ratified.  Keying on the whole header string
            # would then read a block that gained a name as a different
            # canonical and report every model under it as lost.  Index each
            # name separately so the comparison survives the list changing.
            names = [n.strip() for n in ln[4:].split(" (")[0].split(",") if n.strip()]
        elif ln.startswith("## "):
            names = []
        elif names and ("Example models" in ln or ln.lstrip().startswith("- `")):
            found = R_FILE.findall(ln)
            for n in names:
                out[n].update(found)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument(
        "--branch",
        required=True,
        help="consolidation branch; worktree at <repo>/.worktrees/<branch>",
    )
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--pattern", default="origin/claude/*")
    ap.add_argument("--extra-ref", action="append", default=[])
    ap.add_argument("--file", required=True)
    args = ap.parse_args()

    repo = Path(args.repo)
    if not repo.is_dir():
        return fail(f"--repo {repo} is not a directory")
    # Resolve the branch ref FIRST, before anything can return early.
    # `git merge-base --is-ancestor X <unresolvable>` just exits non-zero, which
    # is indistinguishable from "not an ancestor" -- so a bad --branch would
    # silently drop every branch and report a clean register. Fail loudly
    # instead of passing vacuously.
    if not merge_set.resolves(repo, args.branch):
        return fail(
            f"--branch {args.branch!r} does not resolve to a commit in {repo};"
            " refusing to report a vacuous pass."
        )
    if not merge_set.resolves(repo, args.base):
        return fail(f"--base {args.base!r} does not resolve to a commit in {repo}")
    for er in args.extra_ref:
        if er and not merge_set.resolves(repo, er):
            return fail(f"--extra-ref {er!r} does not resolve to a commit in {repo}")
    worktree = repo / ".worktrees" / args.branch
    if not worktree.is_dir():
        return fail(f"no worktree for --branch {args.branch!r} at {worktree}")

    refs = merge_set.candidate_refs(repo, args.pattern, args.extra_ref)
    if not refs:
        return fail(
            f"no branch matches --pattern {args.pattern!r} and no --extra-ref was given;"
            " nothing to verify"
        )

    merged_path = worktree / args.file
    if not merged_path.is_file():
        print(f"    (placement) merged file absent at {merged_path}; skipping.")
        return 0
    merged = parse(merged_path.read_text(errors="replace"))

    # Only branches actually FOLDED IN may be checked.  The queue keeps pushing
    # while a consolidation runs, and merge_branches.sh --exclude-ref leaves
    # branches out on purpose, so the pattern also matches branches that are
    # not in this merge; holding the merge responsible for their content would
    # be a false positive.  merge_set.py decides, from ancestry.
    found = merge_set.compute(repo, args.branch, args.base, refs)
    for line in found.summary():
        print(f"    (placement) {line}")
    if not found.members:
        return fail(
            merge_set.empty_message(args.pattern, args.branch, args.base) + "; nothing to verify"
        )

    gaps: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    for member in found.members:
        txt = merge_set.read_file(repo, member.merged, args.file)
        if not txt:
            continue
        inherited = parse(merge_set.read_file(repo, member.fork, args.file) or "")
        for canon, models in parse(txt).items():
            for m in models - inherited.get(canon, set()) - merged.get(canon, set()):
                gaps[(canon, m)].append(member.ref.replace("origin/", ""))

    if not gaps:
        print(
            f"    (placement) OK — every (canonical, model) pair a branch "
            f"recorded is still filed under that canonical in {args.file}"
        )
        return 0

    print()
    print(
        f"ERROR: (placement) {len(gaps)} (canonical, model) pair(s) a branch "
        f"recorded are NOT filed under that canonical in {args.file}:"
    )
    for (canon, m), brs in sorted(gaps.items()):
        print(
            f"    {canon}  <-  {m}   (from {brs[0]}"
            + (f" +{len(brs) - 1} more" if len(brs) > 1 else "")
            + ")"
        )
    print()
    print("    Usually two branches registered the same canonical from mains lacking each")
    print("    other's copy, and -X theirs kept one entry. Union the surviving entry's")
    print("    Source-aliases and Example-models with the dropped one's, keeping ONE block.")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(2)
    except Exception:
        # Exit 1 means "placements missing"; a crash must not read as that verdict.
        traceback.print_exc()
        sys.exit(2)
