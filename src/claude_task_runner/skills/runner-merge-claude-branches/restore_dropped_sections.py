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

This script performs that pass mechanically: for each branch matching the
pattern, it finds the ``### NAME`` blocks the branch added relative to the
base, and re-inserts any that are missing from the merge result -- into the
same ``## SECTION`` the branch filed them under, creating that section only if
it does not already exist.

Idempotent: a canonical already present is left alone, so re-running is safe.

Usage:
    restore_dropped_sections.py --repo REPO --branch BR --base origin/main \
        --pattern 'origin/claude/*' --file inst/references/covariate-columns.md
    # add --check to report without writing (exit 1 if anything is missing)
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

HEADER_RE = re.compile(r"^### (.+?)(?:\s*\(|\s*$)")
NAME_RE = re.compile(r"^[A-Za-z0-9_<>]+$")
SECTION_RE = re.compile(r"^## +(.*?)\s*$")


def git(args: list[str], cwd: Path) -> str:
    out = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    return out.stdout if out.returncode == 0 else ""


def blocks(text: str) -> dict[str, tuple[str, str, str]]:
    """Map every canonical name -> (parent ``## section``, block text, owning name).

    A multi-name header (``### fm_a, fm_b, fm_c``) contributes one entry per
    name so that "is this canonical present?" is answered correctly; the
    owning name is the first, which is what insertion keys on.
    """
    found: dict[str, tuple[str, str, str]] = {}
    section = ""
    lines = text.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        sec = SECTION_RE.match(lines[i])
        if sec:
            section = sec.group(1)
            i += 1
            continue
        hdr = HEADER_RE.match(lines[i])
        if not hdr:
            i += 1
            continue
        start = i
        i += 1
        while i < len(lines) and not (
            lines[i].startswith("### ") or lines[i].startswith("## ") or lines[i].startswith("# ")
        ):
            i += 1
        names = [n.strip() for n in hdr.group(1).split(",")]
        names = [n for n in names if NAME_RE.match(n)]
        block = "".join(lines[start:i]).rstrip("\n") + "\n"
        for nm in names:
            # Key every name a multi-name header declares, so presence checks
            # answer correctly for `### fm_a, fm_b, fm_c`. The first name owns
            # the block for insertion purposes.
            found.setdefault(nm, (section, block, names[0]))
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
    ap.add_argument("--file", required=True, help="repo-relative path to the register file")
    ap.add_argument(
        "--check", action="store_true", help="report only; exit 1 if anything is missing"
    )
    args = ap.parse_args()

    repo = Path(args.repo)
    worktree = Path(
        git(["worktree", "list", "--porcelain"], repo).split("\n")[0].replace("worktree ", "")
    )
    for line in git(["worktree", "list", "--porcelain"], repo).splitlines():
        if line.startswith("worktree "):
            cand = Path(line[len("worktree ") :])
        elif line == f"branch refs/heads/{args.branch}":
            worktree = cand
            break

    target = worktree / args.file
    if not target.exists():
        print(f"{args.file} not present on {args.branch}; nothing to do")
        return 0

    base_blocks = blocks(git(["show", f"{args.base}:{args.file}"], repo))
    merged_text = target.read_text()
    merged_blocks = blocks(merged_text)

    refs = [
        r
        for r in git(
            [
                "for-each-ref",
                "--format=%(refname:short)",
                f"refs/remotes/{args.pattern.replace('origin/', 'origin/', 1)}",
            ],
            repo,
        ).split()
    ] or git(
        ["for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/claude/"], repo
    ).split()

    # ---- MERGE-SET GATE -----------------------------------------------------
    # Two filters, both required. Without them this script resurrects blocks
    # that were deliberately removed, which is worse than the loss it repairs.
    #
    # (a) ANCESTRY. Only branches actually folded into this consolidation may
    #     contribute. The pattern also matches branches from earlier rounds and
    #     branches pushed after the survey; neither is part of this merge.
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
    #     Tested by CONTENT, not by scanning diffs for removed `### ` lines:
    #     union_merge_lines.py rewrites this file wholesale, so a diff scan
    #     reads every MOVED header as a removal and swallows the real losses
    #     too (it mislabelled 526 blocks that way). Instead, snapshot the file
    #     at the LAST MERGE COMMIT on the branch -- after every branch is
    #     folded in, before any repair ran. A name present there and absent now
    #     was removed on purpose; a name absent there was lost by the merge.
    last_merge = git(["rev-list", "--merges", "-1", args.branch], repo).strip()
    post_merge_blocks = (
        blocks(git(["show", f"{last_merge}:{args.file}"], repo)) if last_merge else {}
    )

    in_merge_set = []
    for ref in refs:
        rc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ref, args.branch],
            cwd=str(repo),
            capture_output=True,
            text=True,
        ).returncode
        if rc == 0:
            in_merge_set.append(ref)
    skipped_refs = len(refs) - len(in_merge_set)

    restored: list[tuple[str, str, str]] = []
    inherited = 0
    deliberate = 0
    lost_names: list[tuple[str, str, str]] = []
    for ref in in_merge_set:
        text = git(["show", f"{ref}:{args.file}"], repo)
        if not text:
            continue
        fork = git(["merge-base", args.base, ref], repo).strip()
        fork_blocks = blocks(git(["show", f"{fork}:{args.file}"], repo)) if fork else {}
        for name, (section, block, owner) in blocks(text).items():
            if name in base_blocks or name in merged_blocks:
                continue
            if name in post_merge_blocks:
                # Survived every merge, then was removed by a repair commit on
                # this branch: a deliberate rename or retirement.
                deliberate += 1
                continue
            if name in fork_blocks:
                # Inherited from the branch's own starting point, not added by
                # it -- main has since renamed or removed it. Not a merge loss.
                inherited += 1
                continue
            if any(name == r[0] for r in restored):
                continue
            if owner != name and owner in merged_blocks:
                # The branch added this name to a header that SURVIVED under a
                # different name, so the block is present but the name was
                # dropped from its header list. Re-inserting the block would
                # duplicate it; the repair is a header edit, so report only.
                lost_names.append((name, owner, ref))
                continue
            restored.append((name, section, block))
            merged_blocks[name] = (section, block, owner)

    if skipped_refs:
        print(
            f"# merge-set gate: {len(in_merge_set)} branch(es) are ancestors of "
            f"{args.branch}; skipped {skipped_refs} matching the pattern but not merged"
        )
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
    for name, owner, ref in lost_names:
        print(
            f"# HEADER-NAME LOST (repair by hand, not a block insert): '{name}' was added to "
            f"the '### {owner}, ...' header by {ref} and is missing from the merged header"
        )
    if lost_names:
        print(
            f"# {len(lost_names)} multi-name header entry(ies) need a manual union; "
            f"buildModelDb() will fail on these until fixed"
        )
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
    sys.exit(main())
