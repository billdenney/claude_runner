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

A model is filed under a canonical when it is an entry of the block's
``**Example models:**`` field, inline or in a sub-bullet (``-`` or ``*``), as
dedup_canonical_headers.py reads entries: a filename named inside another
entry's parentheses, in a source-alias bullet or in Notes prose is not filed
there, so a merge that keeps only a mention of a lost model reports it. Each
source-alias sub-bullet a branch added must also still be under its
canonical, word for word (inside a longer bullet counts), whitespace and
closing punctuation aside.

Exit codes: 0 clean (or the merged file is not in the worktree), 1 missing
placements found, 2 the check could not run: a bad argument, a --base, --branch
or --extra-ref that does not resolve, no worktree for --branch, no branch
matching --pattern, an empty merge set, or a crash.
"""

import argparse
import collections
import sys
import traceback
from collections.abc import Iterator
from pathlib import Path

import dedup_canonical_headers as register
import merge_set


def fail(message: str) -> int:
    print(f"ERROR: (placement) {message}", file=sys.stderr)
    return 2


def _blocks(text: str) -> Iterator[tuple[list[str], register._Block]]:
    """Each ``### `` block of ``text``, with the canonicals its heading names.

    A heading may legitimately name several canonicals that share one block
    ("### QTc, QTcF, QTcI, QTcP, QTcS"), and that list GROWS as new spellings
    are ratified. Keying on the whole heading would read a block that gained
    a name as a different canonical and report every model under it as lost,
    so each name is indexed separately.
    """
    lines = text.split("\n")
    heads, bounds = register._parse(lines)
    for start, _, _ in heads:
        heading = lines[start]
        names = [n.strip() for n in heading[4:].split(" (")[0].split(",") if n.strip()]
        yield names, register._parse_block(lines[start : register._block_end(start, bounds)])


def parse(text: str) -> dict[str, set[str]]:
    """canonical -> the model .R filenames filed under it as Example-models entries."""
    out: dict[str, set[str]] = collections.defaultdict(set)
    for names, block in _blocks(text):
        models = {
            e.model for f in block.named(register.EXAMPLES) for e in register._examples(f).entries
        }
        if models:
            for name in names:
                out[name].update(models)
    return out


def aliases(text: str) -> dict[str, dict[str, str]]:
    """canonical -> its source-alias bullets, each keyed by its words: runs of
    whitespace made one space, and the punctuation that closes it dropped."""
    out: dict[str, dict[str, str]] = collections.defaultdict(dict)
    for names, block in _blocks(text):
        for field in block.named(register.ALIASES):
            bullets = [" ".join(ln.strip() for ln in item)[2:] for item in field.items()]
            if field.inline:
                bullets.append(field.rest)
            for bullet in bullets:
                key = register.CONNECTOR.sub("", register._norm([bullet]))
                for name in names:
                    out[name][key] = bullet
    return out


Read = tuple[dict[str, set[str]], dict[str, dict[str, str]]]


def _read(repo: Path, commit: str, path: str, cache: dict[str, Read]) -> Read | None:
    """``path``'s filings and alias bullets at ``commit``, parsed once per
    blob; None when the commit has no such file, or it is empty."""
    blob = merge_set.blob_id(repo, commit, path)
    if blob is None:
        return None
    if blob not in cache:
        text = merge_set.read_file(repo, commit, path) or ""
        cache[blob] = (parse(text), aliases(text))
    return cache[blob] if any(cache[blob]) else None


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
    merged_text = merged_path.read_text(errors="replace")
    merged = parse(merged_text)
    merged_aliases = aliases(merged_text)

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
    alias_gaps: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    read: dict[str, Read] = {}
    for member in found.members:
        tip = _read(repo, member.merged, args.file, read)
        fork = _read(repo, member.fork, args.file, read)
        if tip is None or tip is fork:
            continue
        branch = member.ref.replace("origin/", "")
        inherited, inherited_aliases = fork or ({}, {})
        models_at_tip, aliases_at_tip = tip
        for canon, models in models_at_tip.items():
            for m in models - inherited.get(canon, set()) - merged.get(canon, set()):
                gaps[(canon, m)].append(branch)
        for canon, bullets in aliases_at_tip.items():
            kept = merged_aliases.get(canon, {})
            for key, bullet in bullets.items():
                # A merged bullet that says more (a hand union of two
                # wordings) still holds every word of this one.
                if key in inherited_aliases.get(canon, {}):
                    continue
                if not any(register._within(key, k) for k in kept):
                    alias_gaps[(canon, bullet)].append(branch)

    if not gaps and not alias_gaps:
        print(
            f"    (placement) OK — every (canonical, model) pair and source alias a branch "
            f"recorded is still filed under that canonical in {args.file}"
        )
        return 0

    if gaps:
        print()
        print(
            f"ERROR: (placement) {len(gaps)} (canonical, model) pair(s) a branch "
            f"recorded are NOT filed under that canonical in {args.file}:"
        )
        _print_gaps(gaps)
        print()
        print("    Usually two branches registered the same canonical from mains lacking each")
        print("    other's copy, and -X theirs kept one entry. Union the surviving entry's")
        print("    Source-aliases and Example-models with the dropped one's, keeping ONE block.")
    if alias_gaps:
        print()
        print(
            f"ERROR: (placement) {len(alias_gaps)} source-alias bullet(s) a branch added are"
            f" NOT under that canonical in {args.file}:"
        )
        _print_gaps(alias_gaps)
    return 1


def _print_gaps(gaps: dict[tuple[str, str], list[str]]) -> None:
    for (canon, what), branches in sorted(gaps.items()):
        more = f" +{len(branches) - 1} more" if len(branches) > 1 else ""
        print(f"    {canon}  <-  {what}   (from {branches[0]}{more})")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(2)
    except Exception:
        # Exit 1 means "placements missing"; a crash must not read as that verdict.
        traceback.print_exc()
        sys.exit(2)
