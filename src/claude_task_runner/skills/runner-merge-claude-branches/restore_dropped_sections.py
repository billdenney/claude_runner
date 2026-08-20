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

HEADER_RE = re.compile(r"^### ([A-Za-z0-9_]+)")
SECTION_RE = re.compile(r"^## +(.*?)\s*$")


def git(args: list[str], cwd: Path) -> str:
    out = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    return out.stdout if out.returncode == 0 else ""


def blocks(text: str) -> dict[str, tuple[str, str]]:
    """Map canonical name -> (parent ``## section`` title, full block text)."""
    found: dict[str, tuple[str, str]] = {}
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
        found[hdr.group(1)] = (section, "".join(lines[start:i]).rstrip("\n") + "\n")
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

    restored: list[tuple[str, str, str]] = []
    for ref in refs:
        text = git(["show", f"{ref}:{args.file}"], repo)
        if not text:
            continue
        for name, (section, block) in blocks(text).items():
            if name in base_blocks or name in merged_blocks:
                continue
            if any(name == r[0] for r in restored):
                continue
            restored.append((name, section, block))
            merged_blocks[name] = (section, block)

    if not restored:
        print(f"# no dropped canonicals in {args.file}")
        return 0

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
