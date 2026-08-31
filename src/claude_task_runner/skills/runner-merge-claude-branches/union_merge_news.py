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
accumulated history, then re-applies every bullet any branch added relative to
base, de-duplicated, inserted under the "# development version" heading.

Idempotent: a bullet already present is not added twice.

Usage:
    union_merge_news.py --repo REPO --branch BR --base origin/main \
        --pattern 'origin/claude/*' [--file NEWS.md] [--check]
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

DEV_HEADING = re.compile(r"^#\s+development version", re.I)


def git(args: list[str], cwd: Path) -> str:
    out = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    return out.stdout if out.returncode == 0 else ""


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


def worktree_for(repo: Path, branch: str) -> Path:
    cand = repo
    for line in git(["worktree", "list", "--porcelain"], repo).splitlines():
        if line.startswith("worktree "):
            cand = Path(line[len("worktree ") :])
        elif line == f"branch refs/heads/{branch}":
            return cand
    return repo


def bullet_ships(block: str, tokens: set[str]) -> bool:
    """True when the bullet's OWN author-year names a model this merge adds.

    Parses the bullet rather than substring-scanning it. A substring test
    false-positives badly on short surnames -- "xu" and "ai" match inside
    squashed compound names like "vandenberg" -- which would let through
    bullets for models the merge does not ship.
    """
    if not tokens:
        return True  # no modeldb diff to gate on; keep prior behaviour
    m = re.match(r"^[-*]\s+(?:Add|Update|Fix)\s+(.+?)\s+((?:19|20)\d{2})\b", block.strip())
    if not m:
        return True  # not an "Add <Author> <Year>" bullet; do not gate it out
    raw = m.group(1).strip()
    # A surname is a few words at most ("van den Berg", "Olsson Gisleskog").
    # A long capture means this is not the per-model form -- e.g. "Add 14
    # published imatinib population PK models transcribed from the Yang 2025
    # external evaluation", where the non-greedy match swallows the whole
    # phrase. Such a bullet legitimately covers many models and MUST NOT be
    # gated out on a failed surname match; keep it.
    if len(raw.split()) > 3:
        return True
    author = re.sub(r"[^a-z]", "", raw.lower())
    year = m.group(2)
    return (author, year) in {(a.replace(" ", ""), y) for a, y in (t.split(" ", 1) for t in tokens)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--branch", required=True)
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--pattern", default="origin/claude/*")
    ap.add_argument("--file", default="NEWS.md")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    repo = Path(args.repo)
    wt = worktree_for(repo, args.branch)
    target = wt / args.file
    if not target.exists():
        print(f"{args.file} not present; nothing to do")
        return 0

    base_text = git(["show", f"{args.base}:{args.file}"], repo)
    if not base_text:
        print(f"cannot read {args.base}:{args.file}; refusing to rewrite", file=sys.stderr)
        return 2
    base_keys = {key(b) for b in bullets(base_text)}

    # Only advertise models this merge actually ships. A branch whose tip
    # advanced after the survey may carry bullets for models that were NOT
    # folded in; adding those would announce models the package does not have.
    shipped = git(
        ["diff", "--name-only", f"{args.base}...{args.branch}", "--", "inst/modeldb/"], repo
    ).split()
    stems = {Path(x).stem.lower() for x in shipped}
    tokens: set[str] = set()
    for st in stems:
        parts = st.split("_")
        if len(parts) >= 2:
            tokens.add(f"{parts[0]} {parts[1]}")

    refs = git(
        ["for-each-ref", "--format=%(refname:short)", f"refs/remotes/{args.pattern}"], repo
    ).split()
    added: list[str] = []
    skipped = 0
    seen = set(base_keys)
    for ref in refs:
        text = git(["show", f"{ref}:{args.file}"], repo)
        if not text:
            continue
        for b in bullets(text):
            k = key(b)
            if k in seen:
                continue
            seen.add(k)
            if not bullet_ships(b, tokens):
                skipped += 1
                continue
            added.append(b)
    if skipped:
        print(f"# skipped {skipped} bullet(s) for models not shipped by this merge")

    current_keys = {key(b) for b in bullets(target.read_text())}
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
    target.write_text(out)
    print(f"# rebuilt {args.file}: {len(base_keys)} base + {len(added)} branch bullets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
