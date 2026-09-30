#!/usr/bin/env python3
"""Union-merge NEWS.md across all folded branches.

NEWS.md has ONE append point (the "# development version" heading), so every
extraction branch adds its bullet to the same few lines. The consolidation
merge therefore conflicts on NEWS.md in nearly every branch, and ``-X theirs``
resolves each conflict by taking the incoming branch's whole copy -- which,
because that branch was cut from an older main, is missing entries main
accumulated since.

The result is doubly destructive and completely silent:

* entries already on main are DELETED (the last-merged branch's older file
  wins outright), and
* every other branch's new bullet is dropped.

Measured on the nlmixr2lib consolidation of 2026-08-20: NEWS.md ended 85 lines
shorter, with 5 reordered duplicates re-added, and NOT ONE of the 169 merged
models had a NEWS entry. A previous round lost 60.

This script rebuilds the file: it takes the BASE version as authoritative for
accumulated history, then re-applies every bullet a merged branch added,
de-duplicated, inserted under the "# development version" heading.

Which bullets a branch added is decided by provenance, not by reading the
bullet. For each ref in the merge set (see merge_set.py) it compares NEWS.md at
the commit the consolidation merged with NEWS.md at that commit's fork point
from the base; the bullets that are new there are the branch's own. A ref the
consolidation did not merge adds nothing, and a ref whose tip moved on after
the merge adds only what the merged part added.

This replaced a gate that parsed "Add <Author> <Year>" and kept a bullet when
some shipped model file had that author and year. It was wrong both ways on
2026-09-29: it kept six bullets from branches left out of the merge ("Add Wang
2020 caspofungin" passed because another Wang 2020 model shipped), and it
dropped bullets whose file stem does not spell the author and year the same
way: a lettered year (Chen_2021a_tacrolimus.R) or a surname particle ("Le
Marouille", Marouille_2021_palbociclib.R).

Idempotent: a bullet already present is not added twice.

Usage:
    union_merge_news.py --repo REPO --branch BR --base origin/main \
        --pattern 'origin/claude/*' [--extra-ref REF ...] [--file NEWS.md] [--check]

Exit codes: 0 done (or the file is not in the worktree); 1 with --check,
bullets are missing; 2 it could not run: a bad argument, a --base, --branch or
--extra-ref that does not resolve, no worktree with --branch checked out, no
branch matching --pattern, an empty merge set, an unreadable base file, or a
crash.
"""

from __future__ import annotations

import argparse
import re
import sys
import traceback
from pathlib import Path

import merge_set

DEV_HEADING = re.compile(r"^#\s+development version", re.I)


# NEWS.md files mix bullet markers -- this package's older entries use "* " and
# newer ones "- ". Matching only "- " silently skipped every "* " bullet, so
# they were never detected as branch additions and never restored (found
# 2026-08-22: Ketharanathan 2023 pentobarbital, whose branch bullet is "* Add
# ...", went missing while the check reported NEWS complete).
BULLET = ("- ", "* ")


def bullets(text: str) -> list[str]:
    """Bullet blocks: a `- ` / `* ` line plus any wrapped continuation lines."""
    blocks: list[str] = []
    cur: list[str] = []
    for line in text.splitlines():
        if line.startswith(BULLET):
            if cur:
                blocks.append("\n".join(cur).rstrip())
            cur = [line]
        elif cur and line.startswith(("  ", "\t")) and line.strip():
            cur.append(line)
        else:
            if cur:
                blocks.append("\n".join(cur).rstrip())
                cur = []
    if cur:
        blocks.append("\n".join(cur).rstrip())
    return blocks


def key(block: str) -> str:
    # Normalise the marker away so the same entry written "- Add X" on one
    # branch and "* Add X" on another is recognised as one bullet.
    k = re.sub(r"^[-*]\s+", "", block.strip())
    return re.sub(r"\s+", " ", k).strip().lower()


def fail(message: str) -> int:
    print(f"ERROR: (news) {message}", file=sys.stderr)
    return 2


def added_by(repo: Path, member: merge_set.Member, file_rel: str) -> list[str]:
    """The bullets ``member`` added: in its merged copy, not at its fork point."""
    merged = merge_set.read_file(repo, member.merged, file_rel)
    if merged is None:
        return []
    inherited = {key(b) for b in bullets(merge_set.read_file(repo, member.fork, file_rel) or "")}
    return [b for b in bullets(merged) if key(b) not in inherited]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--branch", required=True)
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--pattern", default="origin/claude/*")
    ap.add_argument(
        "--extra-ref",
        action="append",
        default=[],
        help="additional ref to include, e.g. a hand-picked branch outside --pattern (repeatable)",
    )
    ap.add_argument("--file", default="NEWS.md")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    repo = Path(args.repo)
    if not repo.is_dir():
        return fail(f"--repo {repo} is not a directory")
    named = [("--base", args.base), ("--branch", args.branch)]
    named += [("--extra-ref", ref) for ref in args.extra_ref if ref]
    for flag, ref in named:
        if not merge_set.resolves(repo, ref):
            return fail(f"{flag} {ref!r} does not resolve to a commit in {repo}")
    wt = merge_set.worktree_of(repo, args.branch)
    if wt is None:
        return fail(f"no worktree of {repo} has --branch {args.branch!r} checked out")
    refs = merge_set.candidate_refs(repo, args.pattern, args.extra_ref)
    if not refs:
        return fail(f"no branch matches --pattern {args.pattern!r} and no --extra-ref was given")
    target = wt / args.file
    if not target.exists():
        print(f"{args.file} not present; nothing to do")
        return 0

    base_text = merge_set.read_file(repo, args.base, args.file)
    if not base_text:
        print(f"cannot read {args.base}:{args.file}; refusing to rewrite", file=sys.stderr)
        return 2
    base_keys = {key(b) for b in bullets(base_text)}

    found = merge_set.compute(repo, args.branch, args.base, refs)
    for line in found.summary():
        print(f"# {line}")
    if not found.members:
        return fail(merge_set.empty_message(args.pattern, args.branch, args.base))

    added: list[str] = []
    seen = set(base_keys)
    for member in found.members:
        for b in added_by(repo, member, args.file):
            k = key(b)
            if k not in seen:
                seen.add(k)
                added.append(b)

    current = bullets(target.read_text(encoding="utf-8"))
    current_keys = {key(b) for b in current}
    lost_from_base = [b for b in bullets(base_text) if key(b) not in current_keys]
    missing_added = [b for b in added if key(b) not in current_keys]

    print(f"# base bullets: {len(base_keys)}   branch-added: {len(added)}")
    print(f"# base bullets missing from the merge result: {len(lost_from_base)}")
    print(f"# branch bullets missing from the merge result: {len(missing_added)}")

    if not lost_from_base and not missing_added:
        print("# NEWS.md already complete; nothing to do")
        return 0
    if args.check:
        return 1

    # Base is authoritative for accumulated history; re-apply every branch bullet.
    # Anything else in the merge result goes: a stale branch's copy brings back
    # bullets main has since reworded or removed. Named, so the drop is not silent.
    dropped = [b for b in current if key(b) not in seen]
    if dropped:
        print(
            f"# dropping {len(dropped)} bullet(s) that are neither on {args.base} nor added by a"
            " merged branch:"
        )
        for b in dropped:
            print(f"#   {b.splitlines()[0]}")
    lines = base_text.splitlines()
    for idx, line in enumerate(lines):
        if DEV_HEADING.match(line):
            insert_at = idx + 1
            break
    else:
        lines = ["# development version", "", *lines]
        insert_at = 1
    payload: list[str] = []
    for b in added:
        payload.extend(["", *b.splitlines()])
    out = "\n".join(lines[:insert_at] + payload + lines[insert_at:]).rstrip("\n") + "\n"
    target.write_text(out, encoding="utf-8")
    print(f"# rebuilt {args.file}: {len(base_keys)} base + {len(added)} branch bullets")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Exit 1 means "bullets missing" under --check; a crash must not read
        # as that verdict.
        traceback.print_exc()
        sys.exit(2)
