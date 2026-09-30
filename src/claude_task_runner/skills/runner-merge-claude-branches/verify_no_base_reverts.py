#!/usr/bin/env python3
"""Report register content on --base that the consolidation merge reverted.

``-X theirs`` resolves every conflicting hunk with the incoming branch's side.
A branch cut from an older main carries older copies of the blocks it
touched, so wherever its hunk overlaps a block main has changed since, the
merge silently puts the older copy back: main's newer lines vanish although
the branch never touched them. None of the other steps notices.
restore_dropped_sections.py restores blocks a branch ADDED, not the base's;
the contribution verifiers check what each branch added; and the R
regeneration can pass.

Two real cases from the 2026-09-29 consolidation (256 branches):

* parameter-names.md: the ``### fm_125d3, fm_1ohm, ... fm_ugt1a1`` block came
  from a branch cut before main added nine pathways to it, so the merge
  dropped those nine names from the heading, with main's example entries, a
  paragraph and a wording fix. 16 models then failed the package's
  convention tests.
* covariate-columns.md: a branch's own commit pasted its new
  ``### CONMED_RTV_CC`` block into the middle of CONMED_RTV_AUC_12H's notes
  line, so the second half of that line ended up in the new block's notes.

What it reports, for each ``--file``: every piece of content the file has on
--base that is absent from the merge result and that no merge-set ref's own
diff removed. A ref's own diff runs from its fork point, ``git merge-base
<base> <merged commit>``, to the commit the consolidation merged (see
merge_set.py). A line a branch deliberately edited or deleted is therefore
not reported; a line the branch never had, because main added it after the
branch forked, is. One exception: a line cut in two -- its first part still
in its block, the rest now ending a line of another block -- is reported even
when a branch's own commit cut it. That is a block pasted into the middle of
a line, as with CONMED_RTV_CC, never an edit.

Content is compared block by block. Each ``### `` block is matched, within its
own ``## `` section, to the result's blocks whose headings share a name with
it: a multi-name heading is split on commas, and its ``(**...**)``
description is not a name. So a block that moved is not a loss, and a token
two sections both use (compartment-names.md does this) is never matched
across them. Within a block:

* each name in the heading, and the heading's description, must survive;
* an Example-models line is compared entry by entry, model file and
  annotation, since the union merger rewrites those lines and adds to them;
  the full stops that end it are not compared;
* every other non-blank line must survive verbatim.

Nothing is repaired: which copy of a block is right needs judgement.

Exit codes: 0 no reverted content (a file that is not on --base, or that a
merged branch deleted, is not checked); 1 reverted content, listed block by
block, or a file on --base the merge result lacks; 2 it could not run: a bad
argument, a --base, --branch or --extra-ref that does not resolve, no worktree
with --branch checked out, no branch matching --pattern, an empty merge set, or
a crash.
"""

from __future__ import annotations

import argparse
import itertools
import sys
import traceback
from collections import Counter, defaultdict
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

import merge_set
from union_merge_lines import EXAMPLE_LINE_RE, split_example_body

Atom = tuple[str, ...]
# How much of a long line a report prints.
SHOWN = 200
# The shortest rest of a line that counts as cut off and moved into another block.
MIN_CUT = 20


def fail(message: str) -> int:
    print(f"ERROR: (reverts) {message}", file=sys.stderr)
    return 2


def heading_names(text: str) -> tuple[frozenset[str], str]:
    """The names a ``### `` heading declares, and its description.

    ``"fm_a, fm_b (**canonical ...**)"`` gives ``({"fm_a", "fm_b"}, "(**canonical ...**)")``.
    """
    names_part, paren, rest = text.partition("(")
    names = frozenset(name.strip() for name in names_part.split(",") if name.strip())
    return names or frozenset({text.strip()}), (paren + rest).strip()


def line_atoms(line: str) -> list[Atom]:
    """What one body line holds: its entries if it lists Example models, else itself."""
    match = EXAMPLE_LINE_RE.match(line)
    entries, tail = split_example_body(match.group(1)) if match else ([], "")
    if not entries:
        return [("line", line)]
    atoms: list[Atom] = []
    for filename, annotation in entries:
        atoms.append(("example", filename))
        if annotation:
            atoms.append(("annotation", filename, annotation))
    # Only prose after the list counts; how many full stops end it does not.
    after = tail.strip(" \t.")
    if after:
        atoms.append(("after", after))
    return atoms


@dataclass
class Unit:
    """A ``### `` block, or a ``## `` section's lines before its first block.

    ``key`` and ``atoms`` are computed on first use, once parsing has added
    every line: most blocks are identical across copies, and matching the
    whole key is enough for those.
    """

    section: str
    title: str
    names: frozenset[str]
    lines: list[str] = field(default_factory=list)

    @cached_property
    def key(self) -> tuple[str, str, tuple[str, ...]]:
        return self.section, self.title, tuple(self.lines)

    @cached_property
    def atoms(self) -> Counter[Atom]:
        atoms: Counter[Atom] = Counter()
        if self.title.startswith("### "):
            names, description = heading_names(self.title[4:])
            atoms.update(("name", name) for name in names)
            if description:
                atoms[("description", description)] += 1
        elif self.title:
            atoms[("section", self.title)] += 1
        for line in self.lines:
            if line:
                atoms.update(line_atoms(line))
        return atoms


def units(text: str) -> list[Unit]:
    section = ""
    current = Unit("", "", frozenset())
    found = [current]
    for raw in text.split("\n"):
        line = raw.rstrip()
        if line.startswith("## "):
            section = line[3:].strip()
            current = Unit(section, line, frozenset())
            found.append(current)
        elif line.startswith("### "):
            current = Unit(section, line, heading_names(line[4:])[0])
            found.append(current)
        else:
            current.lines.append(line)
    return found


class Index:
    """A file's units, found by section and heading name."""

    def __init__(self, found: Sequence[Unit]) -> None:
        self.units = found
        self.exact = {unit.key for unit in found}
        self.by_name: defaultdict[tuple[str, str], list[int]] = defaultdict(list)
        self.leading: defaultdict[str, list[int]] = defaultdict(list)
        for position, unit in enumerate(found):
            if unit.names:
                for name in unit.names:
                    self.by_name[(unit.section, name)].append(position)
            else:
                self.leading[unit.section].append(position)

    def counterparts(self, unit: Unit) -> list[int]:
        """This file's units in ``unit``'s section that share a heading name with it."""
        if not unit.names:
            return self.leading.get(unit.section, [])
        return sorted(
            {i for name in unit.names for i in self.by_name.get((unit.section, name), [])}
        )

    def lost(self, unit: Unit) -> tuple[Counter[Atom], bool]:
        """What of ``unit`` is not in its counterparts here, and whether it has none."""
        if unit.key in self.exact:
            return Counter(), False
        ids = self.counterparts(unit)
        kept: Counter[Atom] = Counter()
        for i in ids:
            kept.update(self.units[i].atoms)
        return unit.atoms - kept, not ids

    def cut_off(self, unit: Unit, line: str) -> str | None:
        """Where the rest of ``line`` went, if a pasted block cut it in two.

        The signature: ``unit``'s counterpart here keeps a first part of the
        line, and the rest of it ends a line of another block. None when the
        line was not cut that way.
        """
        ids = self.counterparts(unit)
        for i in ids:
            for kept in self.units[i].lines:
                if not kept or len(kept) >= len(line) or not line.startswith(kept):
                    continue
                rest = line[len(kept) :].strip()
                if len(rest) < MIN_CUT:
                    continue
                for j, other in enumerate(self.units):
                    if j not in ids and any(text.endswith(rest) for text in other.lines):
                        return other.title or "the text before the first ## section"
        return None


Removals = defaultdict[tuple[str, Atom], list[frozenset[str]]]


def removals(repo: Path, members: Sequence[merge_set.Member], path: str) -> Removals:
    """What each member's own diff removed from ``path``: (section, atom) -> block names.

    Grouped by fork point, so each fork's copy is parsed once; each member's
    copy is parsed, used and dropped, so a large register is never held once
    per member.
    """
    removed: Removals = defaultdict(list)
    touching = sorted(
        (m for m in members if merge_set.touches(repo, m, path)), key=lambda m: m.fork
    )
    for fork, group in itertools.groupby(touching, key=lambda m: m.fork):
        before = units(merge_set.read_file(repo, fork, path) or "")
        for member in group:
            after = Index(units(merge_set.read_file(repo, member.merged, path) or ""))
            for unit in before:
                gone, _ = after.lost(unit)
                for atom in gone:
                    removed[(unit.section, atom)].append(unit.names)
    return removed


def excused(removed: Removals, unit: Unit, atom: Atom) -> bool:
    """True when some member's own diff removed ``atom`` from this block."""
    for names in removed.get((unit.section, atom), []):
        if names & unit.names or not (names or unit.names):
            return True
    return False


@dataclass
class Finding:
    unit: Unit
    missing: list[Atom]
    whole: bool


def check(
    repo: Path, members: Sequence[merge_set.Member], base_text: str, result_text: str, path: str
) -> list[Finding]:
    result = Index(units(result_text))
    losses = []
    for unit in units(base_text):
        gone, whole = result.lost(unit)
        if gone:
            losses.append((unit, gone, whole))
    if not losses:
        return []
    removed = removals(repo, members, path)
    findings = []
    for unit, gone, whole in losses:
        missing: list[Atom] = []
        for atom in gone:
            # A line cut in two is reported even when a branch's own commit cut
            # it: pasting a new block into the middle of a line is an accident,
            # never an edit (CONMED_RTV_CC, 2026-09-29).
            moved_to = result.cut_off(unit, atom[1]) if atom[0] == "line" else None
            if moved_to is not None:
                missing.append(("cut", atom[1], moved_to))
            elif not excused(removed, unit, atom):
                missing.append(atom)
        if missing:
            findings.append(Finding(unit, missing, whole))
    return findings


def _shown(text: str) -> str:
    return text if len(text) <= SHOWN else text[:SHOWN] + " [...]"


def describe(atom: Atom) -> str:
    kind, *rest = atom
    if kind == "section":
        return f"missing section heading: {rest[0]}"
    if kind == "name":
        return f"missing heading name: {rest[0]}"
    if kind == "description":
        return f"missing heading description: {_shown(rest[0])}"
    if kind == "example":
        return f"missing Example-models entry: `{rest[0]}`"
    if kind == "annotation":
        return f"missing Example-models annotation of `{rest[0]}`: {_shown(rest[1])}"
    if kind == "after":
        return f"missing text after the Example-models entries: {_shown(rest[0])}"
    if kind == "cut":
        return f"line cut in two, the rest now ending a line of {rest[1]}: {_shown(rest[0])}"
    return f"missing line: {_shown(rest[0])}"


def report(findings: Sequence[Finding], path: str, base: str) -> Iterator[str]:
    yield ""
    yield f"ERROR: (reverts) {len(findings)} block(s) of {path} lost content that {base} has:"
    for finding in findings:
        unit = finding.unit
        where = f"## {unit.section}" if unit.section else "(before the first ## section)"
        title = unit.title if unit.title.startswith("### ") else "(text before the first ### block)"
        yield f"    {where}"
        yield f"    {title}"
        if finding.whole:
            yield "      the whole block is gone from this section of the merge result"
        for atom in sorted(finding.missing, key=_order):
            yield f"      {describe(atom)}"
    yield ""
    yield "    Usually -X theirs took an older copy of a block from a branch cut before main"
    yield "    changed it; a cut line is a new block pasted into the middle of an old one."
    yield f"    Put {base}'s content back by hand, keeping what the branches added. Nothing"
    yield "    was repaired automatically: which copy is right needs judgement."


_KINDS = ("section", "name", "description", "example", "annotation", "after", "cut", "line")


def _order(atom: Atom) -> tuple[int, Atom]:
    return _KINDS.index(atom[0]), atom


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Report register content on --base that the consolidation merge reverted."
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
        help="repo-relative path to a register file to check (repeatable)",
    )
    args = ap.parse_args(argv)

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
    if not refs:
        return fail(
            f"no branch matches --pattern {args.pattern!r} and no --extra-ref was given;"
            " nothing to verify"
        )

    targets: list[tuple[str, str, str | None]] = []
    for path in args.file:
        base_text = merge_set.read_file(repo, args.base, path)
        result_path = worktree / path
        result_text = result_path.read_text(encoding="utf-8") if result_path.is_file() else None
        if base_text is None:
            print(f"    (reverts) {path} is not on {args.base}; nothing to check.")
            continue
        targets.append((path, base_text, result_text))
    if not targets:
        return 0

    found = merge_set.compute(repo, args.branch, args.base, refs)
    for line in found.summary():
        print(f"    (reverts) {line}")
    if not found.members:
        return fail(
            merge_set.empty_message(args.pattern, args.branch, args.base) + "; nothing to verify"
        )

    reverted = False
    for path, base_text, result_text in targets:
        if result_text is None:
            deleted_by = [
                m.ref
                for m in found.members
                if merge_set.read_file(repo, m.fork, path) is not None
                and merge_set.read_file(repo, m.merged, path) is None
            ]
            if deleted_by:
                print(f"    (reverts) {path} was deleted by {', '.join(deleted_by)}; not checked.")
            else:
                print()
                print(
                    f"ERROR: (reverts) {path} is on {args.base} but not in the merge result,"
                    " and no merged branch deleted it"
                )
                reverted = True
            continue
        findings = check(repo, found.members, base_text, result_text, path)
        if findings:
            reverted = True
            for line in report(findings, path, args.base):
                print(line)
        else:
            print(f"    (reverts) OK — nothing {args.base} has in {path} was reverted by the merge")
    return 1 if reverted else 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:
        # Exit 1 means "content was reverted"; a crash must not read as that verdict.
        traceback.print_exc()
        sys.exit(2)
