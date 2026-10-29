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

A branch can also reword or delete a bullet it inherited (correcting a DOI in
an earlier "Add ..." entry, say). Its fork and merged copies are aligned: a
bullet missing from its copy is paired with the most similar new bullet in the
same stretch, which then replaces it in place in the base's history, and one
with no similar replacement counts as deleted and is removed. A bullet it only
moved is still in its copy and is left alone. A branch that deletes more than
MAX_DELETIONS bullets, or a fifth of those it inherited, has most likely
truncated the file, so the run stops instead. Reading only additions kept both wordings: the base's old
one in place and the branch's new one at the top (seven such duplicates on the
2026-10-10 merge). When the base no longer has the old wording either, main
changed that bullet too; the branch's wording is then added at the top and
named, so the possible duplicate is reviewed.

Idempotent: a bullet already present is not added twice.

Usage:
    union_merge_news.py --repo REPO --branch BR --base origin/main \
        --pattern 'origin/claude/*' [--extra-ref REF ...] [--file NEWS.md] [--check]

Exit codes: 0 done (or the file is not in the worktree); 1 with --check,
bullets are missing or a bullet a branch reworded still has its old wording;
2 it could not run: a bad argument, a --base, --branch or --extra-ref that does
not resolve, no worktree with --branch checked out, no branch matching
--pattern, an empty merge set, an unreadable base file, a branch that deletes
more bullets than MAX_DELETIONS allows, or a crash.
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
import traceback
from pathlib import Path

import merge_set

DEV_HEADING = re.compile(r"^#\s+development version", re.I)
# A branch deleting more inherited bullets than this (or than a fifth of them)
# has most likely truncated or rewritten NEWS.md; applying it would erase history.
MAX_DELETIONS = 10
# How alike an old bullet and a new one must be (difflib ratio of their keys)
# to count as the same entry reworded rather than one deleted and one added.
REWORD_SIMILARITY = 0.6


# NEWS.md files mix bullet markers -- this package's older entries use "* " and
# newer ones "- ". Matching only "- " silently skipped every "* " bullet, so
# they were never detected as branch additions and never restored (found
# 2026-08-22: Ketharanathan 2023 pentobarbital, whose branch bullet is "* Add
# ...", went missing while the check reported NEWS complete).
BULLET = ("- ", "* ")


def bullet_spans(lines: list[str]) -> list[tuple[int, int]]:
    """(first, end) line indices of each bullet block: a `- ` / `* ` line plus
    any wrapped continuation lines; `end` is exclusive."""
    spans: list[tuple[int, int]] = []
    start: int | None = None
    for i, line in enumerate(lines):
        if line.startswith(BULLET):
            if start is not None:
                spans.append((start, i))
            start = i
        elif start is not None and line.startswith(("  ", "\t")) and line.strip():
            continue
        elif start is not None:
            spans.append((start, i))
            start = None
    if start is not None:
        spans.append((start, len(lines)))
    return spans


def bullets(text: str) -> list[str]:
    """Bullet blocks: a `- ` / `* ` line plus any wrapped continuation lines."""
    lines = text.splitlines()
    return ["\n".join(lines[a:b]).rstrip() for a, b in bullet_spans(lines)]


def key(block: str) -> str:
    # Normalise the marker away so the same entry written "- Add X" on one
    # branch and "* Add X" on another is recognised as one bullet.
    k = re.sub(r"^[-*]\s+", "", block.strip())
    return re.sub(r"\s+", " ", k).strip().lower()


def fail(message: str) -> int:
    print(f"ERROR: (news) {message}", file=sys.stderr)
    return 2


def _pair(gone: list[str], fresh: list[str]) -> list[tuple[str, str | None]]:
    """Each removed bullet with the most similar unused new one, or None."""
    pairs: list[tuple[str, str | None]] = []
    used: set[int] = set()
    for old in gone:
        best, best_ratio = None, REWORD_SIMILARITY
        for i, new in enumerate(fresh):
            if i in used:
                continue
            ratio = difflib.SequenceMatcher(None, key(old), key(new), autojunk=False).ratio()
            if ratio >= best_ratio:
                best, best_ratio = i, ratio
        if best is not None:
            used.add(best)
        pairs.append((old, fresh[best] if best is not None else None))
    return pairs


def changes_by(
    repo: Path, member: merge_set.Member, file_rel: str
) -> tuple[list[str], list[tuple[str, str | None]], int]:
    """What ``member`` did to the bullets it inherited at its fork point.

    Returns (added, rewrites, inherited). ``added`` is every bullet in its
    merged copy that the fork point lacks. ``rewrites`` pairs each fork-point
    bullet the merged copy no longer has with the bullet that took its place,
    or None when the branch deleted it: the two copies are aligned bullet by
    bullet, and within each changed stretch a removed bullet is paired with
    the most similar new one (see _pair). A bullet the branch only moved is
    still in its copy and is not a rewrite. ``inherited`` counts the
    fork-point bullets.
    """
    merged = merge_set.read_file(repo, member.merged, file_rel)
    if merged is None:
        return [], [], 0
    old = bullets(merge_set.read_file(repo, member.fork, file_rel) or "")
    new = bullets(merged)
    old_keys = {key(b) for b in old}
    new_keys = {key(b) for b in new}
    added = [b for b in new if key(b) not in old_keys]
    rewrites: list[tuple[str, str | None]] = []
    matcher = difflib.SequenceMatcher(
        None, [key(b) for b in old], [key(b) for b in new], autojunk=False
    )
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            gone = [b for b in old[i1:i2] if key(b) not in new_keys]
            fresh = [b for b in new[j1:j2] if key(b) not in old_keys] if tag == "replace" else []
            rewrites.extend(_pair(gone, fresh))
    return added, rewrites, len(old)


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
    try:
        args.file = merge_set.normalise_path(args.file)
    except merge_set.GitError as err:
        return fail(f"--file: {err}")

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

    # Base bullets a merged branch reworded (old key -> new block) or deleted
    # (old key -> None). The first branch to touch a bullet wins; a second that
    # disagrees is named.
    in_place: dict[str, str | None] = {}
    replacements: set[str] = set()
    notes: list[str] = []
    added: list[str] = []
    for member in found.members:
        branch_added, rewrites, inherited = changes_by(repo, member, args.file)
        deleted = sum(1 for _old, new in rewrites if new is None)
        if deleted > max(MAX_DELETIONS, inherited // 5):
            return fail(
                f"{member.ref} deletes {deleted} of its {inherited} inherited bullets in {args.file};"
                " that looks like a truncated or rewritten file, not deliberate deletions. Fix the"
                " branch, leave it out with --exclude-ref, or merge its NEWS.md by hand."
            )
        for old, new in rewrites:
            k_old = key(old)
            if k_old not in base_keys:
                if new is not None and key(new) not in base_keys:
                    notes.append(
                        f"{member.ref} rewords a bullet {args.base} no longer has; its new wording"
                        f" is added at the top, check it against the base's own: {new.splitlines()[0]}"
                    )
                continue
            if k_old in in_place:
                prev = in_place[k_old]
                if (prev is None) != (new is None) or (prev and new and key(prev) != key(new)):
                    notes.append(
                        f"{member.ref} also changes a bullet another merged branch changed; the"
                        f" first change is kept: {old.splitlines()[0]}"
                    )
                continue
            in_place[k_old] = new
            if new is not None:
                replacements.add(key(new))
        for b in branch_added:
            added.append(b)

    seen = set(base_keys) | replacements
    top: list[str] = []
    for b in added:
        k = key(b)
        if k not in seen:
            seen.add(k)
            top.append(b)
    added = top

    current = bullets(target.read_text(encoding="utf-8"))
    current_keys = {key(b) for b in current}
    lost_from_base = [
        b for b in bullets(base_text) if key(b) not in current_keys and key(b) not in in_place
    ]
    stale_in_place = [
        b for b in bullets(base_text) if key(b) in in_place and key(b) in current_keys
    ]
    missing_added = [b for b in added if key(b) not in current_keys]
    missing_added += [
        new for new in in_place.values() if new is not None and key(new) not in current_keys
    ]

    print(f"# base bullets: {len(base_keys)}   branch-added: {len(added)}")
    print(f"# base bullets missing from the merge result: {len(lost_from_base)}")
    print(f"# branch bullets missing from the merge result: {len(missing_added)}")
    print(f"# base bullets a merged branch reworded or deleted: {len(in_place)}")
    for note in notes:
        print(f"# NOTE: {note}")

    if not lost_from_base and not missing_added and not stale_in_place:
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
    # Rewrite or delete, in place, the base bullets a merged branch changed.
    # Bottom-up, so earlier spans keep their indices.
    for first, end in reversed(bullet_spans(lines)):
        k = key("\n".join(lines[first:end]))
        if k not in in_place:
            continue
        replacement = in_place[k]
        print(f"#   {'reworded' if replacement else 'deleted'} by a merged branch: {lines[first]}")
        if replacement is None:
            # Take a blank separator with it, so no double blank line is left.
            if (
                end < len(lines)
                and not lines[end].strip()
                and (first == 0 or not lines[first - 1].strip())
            ):
                end += 1
            del lines[first:end]
        else:
            lines[first:end] = replacement.splitlines()
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
