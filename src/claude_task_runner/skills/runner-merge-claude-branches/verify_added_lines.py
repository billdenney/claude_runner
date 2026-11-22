#!/usr/bin/env python3
"""Report every line a merged branch added that the merge result lacks.

``-X theirs`` resolves a conflicting hunk with the incoming branch's side. When
two branches each add lines at the same place -- a sentence appended to one
canonical's Notes line, a new ``### `` block after the same neighbour -- the
later merge's hunk overwrites what the earlier branch added there. The other
gates cannot see it: verify_no_base_reverts.py guards only the base's content,
restore_dropped_sections.py restores only blocks whose every name vanished,
and the contribution verifier checks model filenames, headings and source
aliases, not the prose around them. What it catches: sentences several
branches each appended to one canonical's Notes line, of which only the last
survives, and a canonical two branches added in different words at the same
place, of which only the later copy survives, under the same heading.

For each ``--file``, and each ref in the merge set (merge_set.py) whose own
diff touched it, the check reads the file at the ref's fork point and at the
commit the consolidation merged:

* every non-blank line the merged copy adds to a block (the fork point's
  matching block does not keep it) must be kept in the matching block of the
  merge result. Blocks match by a name their ``###`` headings share, in any
  ``##`` section (a hand union may move a canonical), and lines before the
  first ``###`` of a section by the section's name;
* lines of an ``**Example models:**`` field are compared entry by entry
  instead, since union_merge_lines.py rewrites those fields: each entry the
  branch added, or whose note it changed, must still be filed under a ``### ``
  heading that declares one of the names the branch's heading declares, in
  any ``##`` section (a hand union may move a canonical two branches filed
  under different sections), with a note that says at least what the
  branch's says.

Whether a block keeps a line is register_lines.py's rule, which the revert
check uses too: word for word, or, for a field line (``- **Notes:**``), with
each of its sentences whole in one line of the same field, so a hand union
that joins two branches' sentences into one Notes line keeps both. A line a
hand union rewords is reported until it is accepted; that is the decision the
report asks for.

``--set-aside FILE`` names the hand-union report the repairs wrote. A line it
holds verbatim, or an entry it names with its note, is wording a repair
(the dedup, the union, the restore) already set aside there for a hand
union: it is counted, not reported, since ``--hand-union-done`` is the
decision for it.

Nothing is repaired: which wording is right needs judgement.

Exit codes: 0 no added line is missing; 1 lines are missing, listed per file
with the branches that added each; 2 it could not run: a bad argument, a
--base, --branch or --extra-ref that does not resolve, no worktree with
--branch checked out, no branch matching --pattern, an empty merge set, or a
crash.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import dedup_canonical_headers as register
import merge_set
import register_lines
import union_merge_lines as union

# How much of a long line the report prints.
SHOWN = 220

Key = tuple[str, str]
"""A block: ``("###", name)`` for each name its heading declares, or
``("##", section)`` for a section's lines before its first ``###``."""


def fail(message: str) -> int:
    print(f"ERROR: (lines) {message}", file=sys.stderr)
    return 2


@dataclass(frozen=True)
class Copy:
    """One text of a file, read for this check."""

    blocks: dict[Key, register_lines.Block]
    """Its non-blank lines, by block."""
    outside: tuple[tuple[str, tuple[Key, ...]], ...]
    """Its lines outside the Example-models fields, in order, with their blocks."""
    entries: union.Entries
    """Its Example-models entries, ``(bucket, filename) -> note``."""

    def holds(self, line: str, keys: tuple[Key, ...]) -> bool:
        """Whether one of the blocks ``keys`` names keeps ``line``."""
        return any(key in self.blocks and self.blocks[key].holds(line) for key in keys)


def _names(bucket: union.Bucket) -> tuple[str, ...]:
    """The names the bucket's ``### `` heading declares."""
    return register.heading_names(bucket[1])[0] or (bucket[1],)


def _keys(bucket: union.Bucket) -> tuple[Key, ...]:
    if bucket[1]:
        return tuple(("###", name) for name in _names(bucket))
    return (("##", bucket[0]),)


def read_copy(text: str, headings: union.Headings) -> Copy:
    lines = text.split("\n")
    in_field: set[int] = set()
    for _bucket, at, found in union._fields(lines, headings):
        in_field.update(range(at, at + len(found.lines)))
    blocks: defaultdict[Key, register_lines.Block] = defaultdict(register_lines.Block)
    outside: list[tuple[str, tuple[Key, ...]]] = []
    for index, (raw, bucket) in enumerate(
        zip(lines, union._sections(lines, headings), strict=True)
    ):
        line = raw.rstrip()
        if not line.strip():
            continue
        keys = _keys(bucket)
        for key in keys:
            blocks[key].add([line])
        if index not in in_field:
            outside.append((line, keys))
    return Copy(dict(blocks), tuple(outside), union.entry_map(text, headings))


def _filed(entries: union.Entries) -> dict[tuple[str, str], list[str]]:
    """``(heading name, filename) -> notes``: each model filed under each name,
    in whatever ``##`` section."""
    out: dict[tuple[str, str], list[str]] = defaultdict(list)
    for (bucket, filename), note in entries.items():
        for name in _names(bucket):
            out[(name, filename)].append(note)
    return out


def _entry_text(bucket: union.Bucket, filename: str, note: str) -> str:
    label = " / ".join(part for part in (bucket[0], ", ".join(_names(bucket))) if part)
    shown = f"`{filename}`" + (f" {note.strip()}" if note.strip() else "")
    return f"Example models of {label}: {shown}"


def _norm(text: str) -> str:
    return " ".join(text.split())


class Result:
    """The merge result, read for the lines and entries it holds."""

    def __init__(self, text: str, headings: union.Headings) -> None:
        self.copy = read_copy(text, headings)
        self.filed = _filed(self.copy.entries)

    def has_line(self, line: str, bucket: union.Bucket) -> bool:
        """Whether the result block matching ``bucket`` keeps ``line``."""
        return self.copy.holds(line, _keys(bucket))

    def has_entry(self, bucket: union.Bucket, filename: str, note: str) -> bool:
        notes = [kept for name in _names(bucket) for kept in self.filed.get((name, filename), [])]
        return any(union._same(kept, note) or union._contains(kept, note) for kept in notes)


class SetAside:
    """What the repairs' hand-union report holds, verbatim."""

    def __init__(self, text: str) -> None:
        self.lines = [_norm(line) for line in text.splitlines() if line.strip()]
        self.exact = frozenset(self.lines)

    def has_line(self, line: str) -> bool:
        return _norm(line) in self.exact

    def has_entry(self, filename: str, note: str) -> bool:
        name = f"`{filename}`"
        bare = _norm(register._bare(note))
        return any(name in line and bare in line for line in self.lines)


@dataclass
class Findings:
    lost: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    """Each added line (or Example-models entry, as text) the result lacks,
    with the refs that added it, in the order first seen."""
    set_aside: int = 0
    """How many more a repair set aside in the hand-union report."""


def missing(
    repo: Path,
    members: Sequence[merge_set.Member],
    path: str,
    result_text: str,
    set_aside: SetAside | None = None,
) -> Findings:
    """What each member added to ``path`` that the result lacks."""
    headings = union.headings_of(result_text)
    result = Result(result_text, headings)
    copies: dict[str, Copy] = {}

    def copy_at(commit: str) -> Copy | None:
        blob = merge_set.blob_id(repo, commit, path)
        if blob is None:
            return None
        if blob not in copies:
            copies[blob] = read_copy(merge_set.git(repo, "cat-file", "blob", blob), headings)
        return copies[blob]

    found = Findings()
    aside: set[str] = set()

    def note_loss(text: str, ref: str, repaired_aside: bool) -> None:
        if repaired_aside:
            aside.add(text)
        elif ref not in found.lost[text]:
            found.lost[text].append(ref)

    for member in members:
        if not merge_set.touches(repo, member, path):
            continue
        merged = copy_at(member.merged)
        if merged is None:
            continue
        fork = copy_at(member.fork) or Copy({}, (), union.Entries())
        ref = member.ref.removeprefix("origin/")
        for line, keys in merged.outside:
            if fork.holds(line, keys) or result.copy.holds(line, keys):
                continue
            note_loss(line, ref, set_aside is not None and set_aside.has_line(line))
        for (bucket, filename), note in merged.entries.items():
            inherited = fork.entries.get((bucket, filename))
            if inherited is not None and union._same(inherited, note):
                continue
            if result.has_entry(bucket, filename, note):
                continue
            note_loss(
                _entry_text(bucket, filename, note),
                ref,
                set_aside is not None and set_aside.has_entry(filename, note),
            )
    found.set_aside = len(aside - found.lost.keys())
    found.lost = dict(found.lost)
    return found


def report(path: str, lost: dict[str, list[str]]) -> list[str]:
    out = [
        "",
        f"ERROR: (lines) {len(lost)} line(s) a merged branch added are missing from {path}:",
    ]
    for line, refs in lost.items():
        shown = line if len(line) <= SHOWN else line[: SHOWN - 3] + "..."
        out.append(f"    [{', '.join(refs)}] {shown}")
    out += [
        "",
        "    A later merge's -X theirs hunk overwrote each, or a repair or hand edit",
        "    reworded it. Put each back, or union it into the line that replaced it.",
    ]
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Report every line a merged branch added that the merge result lacks."
    )
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
    ap.add_argument(
        "--file",
        action="append",
        required=True,
        help="repo-relative path to a file to check (repeatable)",
    )
    ap.add_argument(
        "--set-aside",
        metavar="FILE",
        help="the hand-union report the repairs wrote: a line it holds verbatim is wording a"
        " repair set aside for a hand union, counted rather than reported",
    )
    merge_set.add_record_argument(ap)
    args = ap.parse_args(argv)
    try:
        args.file = [merge_set.normalise_path(path) for path in args.file]
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
    worktree = merge_set.worktree_of(repo, args.branch)
    if worktree is None:
        return fail(f"no worktree of {repo} has --branch {args.branch!r} checked out")
    refs = merge_set.candidate_refs(repo, args.pattern, args.extra_ref)
    record = merge_set.read_record(args.merged_record) if args.merged_record else None
    if not refs and record is None:
        return fail(
            f"no branch matches --pattern {args.pattern!r} and no --extra-ref was given;"
            " nothing to verify"
        )
    found = merge_set.compute(repo, args.branch, args.base, refs, record)
    for line in found.summary():
        print(f"    (lines) {line}")
    if not found.members:
        return fail(
            merge_set.empty_message(args.pattern, args.branch, args.base) + "; nothing to verify"
        )

    set_aside = None
    if args.set_aside:
        report_path = Path(args.set_aside)
        if report_path.is_file():
            set_aside = SetAside(report_path.read_text(encoding="utf-8"))

    any_lost = False
    for path in dict.fromkeys(args.file):
        result_path = worktree / path
        result_text = result_path.read_text(encoding="utf-8") if result_path.is_file() else ""
        findings = missing(repo, found.members, path, result_text, set_aside)
        if findings.lost:
            any_lost = True
            print("\n".join(report(path, findings.lost)))
        else:
            print(
                f"    (lines) OK — every line a merged branch added to {path} is in the merge result"
            )
        if findings.set_aside:
            print(
                f"    (lines) {findings.set_aside} more from {path} a repair set aside in the"
                " hand-union report, for the hand union"
            )
    return 1 if any_lost else 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:
        # Exit 1 is the verdict "lines missing"; a crash must not read as it.
        traceback.print_exc()
        sys.exit(2)
