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

Exit codes: 0 clean, 1 missing placements found, 2 bad args.
"""

import argparse
import collections
import re
import subprocess
import sys
from pathlib import Path

R_FILE = re.compile(r"`([A-Za-z0-9_.\-]+\.R)`")


def git(args: list[str], repo: Path) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True).stdout


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
    merged_path = repo / ".worktrees" / args.branch / args.file
    if not merged_path.is_file():
        print(f"    (placement) merged file absent at {merged_path}; skipping.")
        return 0
    merged = parse(merged_path.read_text(errors="replace"))

    refs = [
        r
        for r in git(
            ["for-each-ref", "--format=%(refname:short)", f"refs/remotes/{args.pattern}"], repo
        ).split()
        if r
    ]
    for er in args.extra_ref:
        if er and er not in refs:
            refs.append(er)

    # Only branches actually FOLDED IN may be checked.  The queue keeps pushing
    # while a consolidation runs, so the pattern also matches branches that
    # appeared after the survey and are not in this merge; holding the merge
    # responsible for their content would be a false positive.  Step 4 uses real
    # merges, so "folded in" == "is an ancestor of the consolidation branch".
    # Resolve the branch ref FIRST. `git merge-base --is-ancestor X <unresolvable>`
    # just exits non-zero, which is indistinguishable from "not an ancestor" --
    # so a bad --branch would silently drop every branch and report a clean
    # register. Fail loudly instead of passing vacuously.
    probe = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{args.branch}^{{commit}}"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0 or not probe.stdout.strip():
        print(
            f"ERROR: (placement) --branch {args.branch!r} does not resolve to a "
            f"commit in {repo}; refusing to report a vacuous pass.",
            file=sys.stderr,
        )
        return 2

    merged_refs = []
    skipped = 0
    for r in refs:
        rc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", r, args.branch], cwd=repo, capture_output=True
        )
        if rc.returncode == 0:
            merged_refs.append(r)
        else:
            skipped += 1
    refs = merged_refs
    if skipped:
        print(
            f"    (placement) ignoring {skipped} branch(es) matching the "
            f"pattern that are not ancestors of {args.branch} "
            f"(pushed after the survey; not in this merge)"
        )

    gaps: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    for br in refs:
        txt = git(["show", f"{br}:{args.file}"], repo)
        if not txt:
            continue
        for canon, models in parse(txt).items():
            for m in models - merged.get(canon, set()):
                gaps[(canon, m)].append(br.replace("origin/", ""))

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
