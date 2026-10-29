#!/usr/bin/env python3
"""Restore whole ``### CANONICAL`` blocks that ``-X theirs`` dropped during a merge.

``union_merge_lines.py`` reconstructs the ``**Example models:**`` LINES inside
buckets that already exist on the merge result. It cannot recover a canonical
whose entire ``### NAME`` block is absent -- and that is a routine outcome of
the consolidation merge: when a branch ADDS a brand-new canonical and a later
branch (based on an older main, so lacking it) touches the same region,
``-X theirs`` takes the later branch's copy of the hunk and the new block
disappears. ``verify_branch_contributions.sh`` reports the loss but does not
repair it, which is why every large consolidation has needed a manual
"hand-restore the new headers" pass.

This script performs that pass mechanically: for each branch in the merge set
(merge_set.py: the refs matching the pattern that the consolidation merged,
each read at the commit it merged), it finds the ``### NAME`` blocks the
branch added relative to its fork point from the base, and re-inserts any that
are missing from the merge result -- into the same ``## SECTION`` the branch
filed them under, creating that section only if it does not already exist.

Every ``### `` heading counts, a backticked name or prose included. A heading's
names are read by ``heading_names``, as nlmixr2lib's register parsers read them:
a backticked name keeps its backticks, so ``### `prob` `` is not ``### prob``.
When two branches added the same canonical in different words and the merge lost it, the first branch's copy is
restored and each other copy is printed in full, and with ``--report FILE``
appended to the hand-union report, for a hand union. A later heading that
shares any name with the restored one is such a copy, whatever order it lists
the names in. So is a name a branch added to a multi-name heading that
survived without it: the block is there, so the repair is the heading, by hand.

Idempotent: a canonical already present is left alone, so re-running is safe.

Usage:
    restore_dropped_sections.py --repo REPO --branch BR --base origin/main \
        --pattern 'origin/claude/*' [--extra-ref REF ...] \
        --file inst/references/covariate-columns.md
    # add --check to report without writing (exit 1 if anything is missing)

Exit codes: 0 done (or the file is not in the worktree); 1 with --check,
something is missing; 2 it could not run: a bad argument, a --base, --branch or
--extra-ref that does not resolve, no worktree with --branch checked out, no
branch matching --pattern, an empty merge set, or a crash.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import traceback
from pathlib import Path

import dedup_canonical_headers as register
import merge_set

RESTORE_INTRO = (
    "Two merged branches added each canonical below in different words, and the",
    "merge lost both. The restore put the first branch's copy back; the other is",
    "below, verbatim. Union it into the register by hand, keeping each source's",
    "attribution, and commit.",
)

NAME_INTRO = (
    "A merged branch added each name below to a heading that the merge kept",
    "without it; the block is in the register under its other names. Add the name",
    "to the register's heading by hand and commit: buildModelDb() fails until then.",
)

SECTION_RE = re.compile(r"^## +(.*?)\s*$")


def git(args: list[str], cwd: Path) -> str:
    out = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    return out.stdout if out.returncode == 0 else ""


def fail(message: str) -> int:
    print(f"ERROR: (restore) {message}", file=sys.stderr)
    return 2


def blocks(text: str) -> dict[str, tuple[str, str, tuple[str, ...]]]:
    """Map every canonical name -> (parent ``## section``, block text, the
    names its heading declares, in order).

    A multi-name header (``### fm_a, fm_b, fm_c``) contributes one entry per
    name so that "is this canonical present?" is answered correctly; the
    owning name is the first, which is what insertion keys on.
    """
    found: dict[str, tuple[str, str, tuple[str, ...]]] = {}
    section = ""
    lines = text.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        sec = SECTION_RE.match(lines[i])
        if sec:
            section = sec.group(1)
            i += 1
            continue
        heading = lines[i][4:].strip() if lines[i].startswith("### ") else ""
        if not heading:
            i += 1
            continue
        start = i
        i += 1
        while i < len(lines) and not (
            lines[i].startswith("### ") or lines[i].startswith("## ") or lines[i].startswith("# ")
        ):
            i += 1
        names = register.heading_names(heading)[0]
        block = "".join(lines[start:i]).rstrip("\n") + "\n"
        for nm in names:
            # Key every name a multi-name header declares, so presence checks
            # answer correctly for `### fm_a, fm_b, fm_c`. The first name owns
            # the block for insertion purposes.
            found.setdefault(nm, (section, block, names))
    return found


def insert(text: str, section: str, block: str) -> str:
    """Append ``block`` to the end of ``## section``; create the section if absent."""
    lines = text.splitlines(keepends=True)
    sec_start = None
    for idx, line in enumerate(lines):
        m = SECTION_RE.match(line)
        if m and m.group(1) == section:
            sec_start = idx
            break
    if sec_start is None:
        tail = "" if text.endswith("\n") else "\n"
        return text + f"{tail}\n## {section}\n\n{block}"
    end = len(lines)
    for idx in range(sec_start + 1, len(lines)):
        if SECTION_RE.match(lines[idx]) or lines[idx].startswith("# "):
            end = idx
            break
    while end > sec_start + 1 and not lines[end - 1].strip():
        end -= 1
    return "".join(lines[:end]) + "\n" + block + "".join(lines[end:])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument(
        "--branch", required=True, help="the consolidation branch (worktree checked out)"
    )
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--pattern", default="origin/claude/*")
    ap.add_argument(
        "--extra-ref",
        action="append",
        default=[],
        help="additional ref to include, e.g. a hand-picked branch outside --pattern (repeatable)",
    )
    ap.add_argument("--file", required=True, help="repo-relative path to the register file")
    ap.add_argument(
        "--check", action="store_true", help="report only; exit 1 if anything is missing"
    )
    ap.add_argument(
        "--report",
        metavar="FILE",
        help="append each copy the restore did not put back, beside the one it did, to FILE"
        " for a hand union (not with --check)",
    )
    args = ap.parse_args()

    repo = Path(args.repo)
    if not repo.is_dir():
        return fail(f"--repo {repo} is not a directory")
    named = [("--base", args.base), ("--branch", args.branch)]
    named += [("--extra-ref", ref) for ref in args.extra_ref if ref]
    for flag, ref in named:
        if not merge_set.resolves(repo, ref):
            return fail(f"{flag} {ref!r} does not resolve to a commit in {repo}")
    # No fallback: guessing another worktree would read, and write, the wrong file.
    worktree = merge_set.worktree_of(repo, args.branch)
    if worktree is None:
        return fail(f"no worktree of {repo} has --branch {args.branch!r} checked out")

    refs = merge_set.candidate_refs(repo, args.pattern, args.extra_ref)
    if not refs:
        return fail(f"no branch matches --pattern {args.pattern!r} and no --extra-ref was given")

    target = worktree / args.file
    if not target.exists():
        print(f"{args.file} not present on {args.branch}; nothing to do")
        return 0

    base_blocks = blocks(git(["show", f"{args.base}:{args.file}"], repo))
    merged_text = target.read_text()
    merged_blocks = blocks(merged_text)

    # ---- MERGE-SET GATE -----------------------------------------------------
    # Two filters, both required. Without them this script resurrects blocks
    # that were deliberately removed, which is worse than the loss it repairs.
    #
    # (a) THE MERGE SET (merge_set.py). Only branches actually folded into this
    #     consolidation may contribute. The pattern also matches branches from
    #     earlier rounds, branches left out with --exclude-ref and branches
    #     pushed after the survey; none is part of this merge. A branch whose
    #     tip moved on after it was merged contributes the part that was merged.
    #
    # (b) FORK POINT. Only blocks the branch ADDED count. Comparing against the
    #     CURRENT base is not enough: when main RENAMES a canonical, every
    #     branch cut before the rename still carries the old spelling in its
    #     copy of the file. That name is absent from the current base (renamed
    #     away) and absent from the merge result (correctly dropped), so the
    #     old test restored it. Comparing against the branch's own merge-base
    #     shows it was inherited, not added, and skips it.
    #
    #     Measured 2026-09-12 on an 80-branch round: of 31 blocks "restored",
    #     20 were such rename zombies (CONMED_RTV_AUC_12h, viralLoad, ooc1..4,
    #     pappBa/pappAb, 13 more camelCase PD outputs). Every one had its
    #     renamed successor already present in the merge result.
    # (c) DELIBERATE REMOVAL ON THE CONSOLIDATION BRANCH. A reconciliation
    #     commit on the branch may rename or retire a canonical after the
    #     merges land (e.g. applying an operator naming ruling that post-dates
    #     the branch). The name is then genuinely "added by a folded branch and
    #     absent from the result", and must still not be restored or every
    #     re-run undoes the rename.
    #
    #     Tested by comparing PARSED BLOCK SETS across each NON-MERGE commit on
    #     the branch: a name present in a commit's parent and absent in the
    #     commit itself was removed by that commit. Two things this gets right
    #     that simpler tests do not:
    #
    #       - Not a diff scan for removed `### ` lines. union_merge_lines.py
    #         rewrites the file wholesale, so a diff reads every MOVED header as
    #         a removal; an earlier draft mislabelled 526 blocks that way.
    #         Comparing parsed sets is immune to moves.
    #       - Only NON-MERGE commits count. A name can also disappear at a later
    #         MERGE (branch A adds it, branch B is folded in afterwards and
    #         -X theirs takes B's copy) -- that is the real loss this script
    #         exists to repair, and must not be mistaken for a deliberate one.
    #         Testing "present at any merge commit" gets this backwards.
    deliberately_removed: set[str] = set()
    for sha in git(
        ["rev-list", "--no-merges", f"{args.base}..{args.branch}", "--", args.file], repo
    ).split():
        after = blocks(git(["show", f"{sha}:{args.file}"], repo))
        before = blocks(git(["show", f"{sha}^:{args.file}"], repo))
        deliberately_removed.update(set(before) - set(after))

    found = merge_set.compute(repo, args.branch, args.base, refs)
    for line in found.summary():
        print(f"# {line}")
    if not found.members:
        return fail(merge_set.empty_message(args.pattern, args.branch, args.base))

    restored: list[tuple[str, str, str]] = []
    restored_from: dict[str, tuple[str, str, str]] = {}
    """Every name a restored heading declares -> the restored block's owning
    name, the branch it came from, and the block. Keyed by every name, so a
    later copy is matched whatever its heading leads with."""
    differing: list[tuple[str, str, str, str, str]] = []
    inherited = 0
    deliberate = 0
    lost_names: list[tuple[str, str, str, str]] = []
    for member in found.members:
        ref = member.ref
        text = merge_set.read_file(repo, member.merged, args.file)
        if not text:
            continue
        fork_blocks = blocks(merge_set.read_file(repo, member.fork, args.file) or "")
        branch_blocks = blocks(text)
        for name, (section, block, names) in branch_blocks.items():
            owner = names[0]
            added = [n for n in names if n not in base_blocks]
            earlier = next((restored_from[n] for n in added if n in restored_from), None)
            if earlier is not None:
                # A copy of a block already restored: its heading shares a name
                # the base lacks with the restored one, in any order (a name the
                # base has was added by no branch, even when a restored heading
                # declares it too). Compared once, at the first such name;
                # restoring it too would declare that name twice.
                kept, first, first_block = earlier
                if name == added[0] and block != first_block:
                    branch = ref.replace("origin/", "")
                    differing.append((kept, branch, first, first_block, block))
                continue
            if name in base_blocks or name in merged_blocks:
                continue
            if name in deliberately_removed:
                # Present in the file at some merge commit on this branch and
                # absent now: a repair commit removed it on purpose (a rename
                # or a retirement), so restoring it would undo that.
                deliberate += 1
                continue
            if name in fork_blocks:
                # Inherited from the branch's own starting point, not added by
                # it -- main has since renamed or removed it. Not a merge loss.
                inherited += 1
                continue
            survivor = next((n for n in names if n != name and n in merged_blocks), None)
            if survivor is not None:
                # The branch added this name to a header that SURVIVED under
                # another of its names, first or not, so the block is present
                # but the name was dropped from its header list. Re-inserting
                # the block would duplicate it; the repair is a header edit,
                # so report only.
                lost_names.append((name, survivor, ref.replace("origin/", ""), block))
                continue
            restored.append((name, section, block))
            # Every name the restored heading declares is present now.
            for other in names:
                restored_from[other] = (owner, ref.replace("origin/", ""), block)
                merged_blocks[other] = (section, block, names)

    if deliberate:
        print(
            f"# merge-set gate: skipped {deliberate} block(s) removed by a non-merge commit "
            f"ON {args.branch} itself (deliberate rename/retire) -- NOT merge losses"
        )
    if inherited:
        print(
            f"# merge-set gate: skipped {inherited} block(s) present at a branch's own "
            f"fork point (inherited, then renamed/removed on {args.base}) -- NOT merge losses"
        )
    for name, owner, ref, _ in lost_names:
        print(
            f"# HEADER-NAME LOST (repair by hand, not a block insert): '{name}' was added to "
            f"the '### {owner}, ...' header by {ref} and is missing from the merged header"
        )
    if lost_names:
        print(
            f"# {len(lost_names)} multi-name header entry(ies) need a manual union; "
            f"buildModelDb() will fail on these until fixed"
        )
    for name, ref, first, _, block in differing:
        print(
            f"# {name}: {ref} added it too, worded differently; the restore takes"
            f" {first}'s copy. Union this one by hand:"
        )
        print("".join(f"    {line}" for line in block.splitlines(keepends=True)), end="")
    if differing and args.report is not None and not args.check:
        hand = [
            register.HandUnion(
                name,
                [],
                [
                    register.Difference(
                        f"{ref}'s copy (the register has {first}'s)",
                        first_block.splitlines(),
                        block.splitlines(),
                    )
                ],
            )
            for name, ref, first, first_block, block in differing
        ]
        register.append_report(args.report, args.file, hand, RESTORE_INTRO, "In that copy:")
    if lost_names and args.report is not None and not args.check:
        lost = [
            register.HandUnion(
                owner,
                [],
                [
                    register.Difference(
                        f"heading name {name}, which {ref} added",
                        merged_blocks[owner][1].splitlines()[:1],
                        block.splitlines()[:1],
                    )
                ],
            )
            for name, owner, ref, block in lost_names
        ]
        register.append_report(args.report, args.file, lost, NAME_INTRO, "In that branch's copy:")
    if not restored:
        print(f"# no dropped canonicals in {args.file}")
        return 1 if (args.check and lost_names) else 0

    print(f"# {len(restored)} canonical(s) dropped by the merge and missing from {args.file}:")
    for name, section, _ in restored:
        print(f"    {name}  (## {section})")

    if args.check:
        return 1

    for _name, section, block in restored:
        merged_text = insert(merged_text, section, block)
    target.write_text(merged_text)
    print(f"# restored {len(restored)} block(s) into {target}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Exit 1 means "something is missing" under --check; a crash must not
        # read as that verdict.
        traceback.print_exc()
        sys.exit(2)
