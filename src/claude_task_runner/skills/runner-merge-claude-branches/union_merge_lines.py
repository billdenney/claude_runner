#!/usr/bin/env python3
"""Union-merge a structured-markdown file across multiple branches.

The use case: a project-wide reference file (e.g.
``inst/references/covariate-columns.md`` in nlmixr2lib) has lines
of the shape::

    - **Example models:** `Author_Year_drug.R` (annotation), `B.R` (annotation), ...

Multiple branches each append their own model to the SAME line. A
bulk merge with ``-X theirs`` only keeps the last branch's version,
silently losing every other branch's annotations.

This script repairs that loss after the merge:

1. Reads the file at ``--base``.
2. Finds the branches in the merge set (see merge_set.py) whose own diff
   touched the file, and reads each one's copy at the commit that was
   merged. A ref the pattern matches but the consolidation did not merge
   contributes nothing: on 2026-09-29 the refs left out with
   ``--exclude-ref`` put orphan Example-models entries into a merge that
   did not ship their models. A ref whose tip moved on after the merge
   contributes only the part that was merged.
3. Reads every ``**Example models:**`` field with the dedup's parser
   (dedup_canonical_headers.py: the whole field, label line, wrapped lines
   and sub-bullets) and buckets the ``(filename, note)`` entries by
   ``(covariate header, subsection header)`` resolved from the most recent
   ``##``/``###`` markdown headings, where a copy's ``###`` heading that
   declares one of the names of a heading in the merge result stands for
   that heading: a branch may add a name to it, reorder its names or reword
   its description while another adds a model under it. An entry's note is what follows its
   filename up to the next entry, a ``;`` or the full stop that ends a
   sentence, the same wherever the entry stands: a closing remark belongs to
   the field. Notes are compared by what they say, not by their form.
4. Builds a union of the base's entries and those each branch changed:
   ones it added, or whose annotation it changed, relative to its fork
   point. An entry a branch only inherited is main's; main may have
   renamed or removed it since. The longest (most informative)
   note per filename per bucket wins, except where a branch changed
   a note the base still has as the branch inherited it: that
   correction wins whatever its length. Every other note "longest wins"
   sets aside goes to the hand-union report (``--report FILE``), beside the
   one kept, or to stderr without it, and so does prose a branch added to a
   field that the result lacks. An entry a branch removed (a
   rename, a re-filing under another heading) is dropped when the base
   still has it, so the merge does not put the old name back.
5. Walks the CURRENT (post-merge) file and edits each Example-models field
   the union changes in place: its entries keep their places and their
   text, prose before, between and after them stays, a model listed twice
   stays listed twice, and new entries go after the field's last entry in
   its form. A field the union changes nothing in is left byte-for-byte as
   it is, so a second run changes nothing.
6. Writes the result back to the merge branch's worktree.

Out-of-scope additions (brand-new lines, new sections, table rows
that aren't Example-models fields) are NOT touched — the merge
already handles those via standard 3-way merging because they
appear at unique line positions per branch. Neither is an Example-models
field that lists no model, such as "none yet".

Exit codes: 0 done (including nothing to reconstruct, the file not in the
worktree, or notes set aside for a hand union); 2 it could not run: a bad
argument, a ``--base``, ``--branch`` or ``--extra-ref`` that does not resolve,
no worktree for ``--branch``, no branch matching ``--pattern``, an empty merge
set, or a failed git command.
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
import traceback
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import dedup_canonical_headers as register
import merge_set

SECTION_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

Bucket = tuple[str, str]

Headings = dict[tuple[str, str], str]
"""``(## section, name) -> the ### heading that declares it`` in the file the
union edits (see :func:`headings_of`)."""

UNION_INTRO = (
    "Two merged copies of the register annotate each model below differently.",
    "The union kept the longer annotation, or a branch's correction; the other",
    "is below, verbatim. Union it into the register by hand, keeping each",
    "source's attribution, and commit.",
)


def fail(message: str) -> int:
    sys.stderr.write(f"ERROR: (union-merge) {message}\n")
    return 2


def _sections(lines: Sequence[str], headings: Headings | None = None) -> list[Bucket]:
    """``(cov, sub)`` for each line: the latest ``##`` heading and the latest deeper one.

    The bucket key is the pair, so the same subsection name under different
    covariates is kept distinct. With ``headings`` (the edited file's), a
    ``###`` heading that declares one of its names stands for that file's
    heading: one branch may add a name to a heading, reorder its names or
    reword its description while another files a model under it, and both
    fields are the one canonical's.
    """
    cov = ""
    sub = ""
    found: list[Bucket] = []
    for line in lines:
        match = SECTION_RE.match(line)
        if match:
            depth = len(match.group(1))
            if depth == 2:
                cov = match.group(2)
                sub = ""
            elif depth >= 3:
                sub = match.group(2)
                if depth == 3 and headings:
                    sub = _resolved(cov, sub, headings)
        found.append((cov, sub))
    return found


def headings_of(text: str) -> Headings:
    """Each name a ``###`` heading of ``text`` declares, under its ``##``
    section, mapped to that heading (the first, when two declare it)."""
    lines = text.split("\n")
    out: Headings = {}
    for line, (cov, sub) in zip(lines, _sections(lines), strict=True):
        match = SECTION_RE.match(line)
        if match and len(match.group(1)) == 3:
            for name in register.heading_names(sub)[0]:
                out.setdefault((cov, name), sub)
    return out


def _resolved(cov: str, heading: str, headings: Headings) -> str:
    """The edited file's heading under ``cov`` that declares one of
    ``heading``'s names: ``heading`` itself when it is one of them, the first
    in sorted order otherwise, and ``heading`` when there is none."""
    found = {headings[(cov, n)] for n in register.heading_names(heading)[0] if (cov, n) in headings}
    if heading in found or not found:
        return heading
    return min(found)


def _fields(
    lines: list[str], headings: Headings | None = None
) -> Iterator[tuple[Bucket, int, register._Field]]:
    """Every Example-models field of every ``### `` block in ``lines``, read
    by the dedup's parser: its bucket (see :func:`_sections`), the index of
    its label line, the field."""
    buckets = _sections(lines, headings)
    heads, bounds = register._parse(lines)
    for head, _, _ in heads:
        block = register._parse_block(lines[head : register._block_end(head, bounds)])
        at = head + 1 + len(block.preamble)
        for found in block.fields:
            if found.name == register.EXAMPLES:
                yield buckets[at], at, found
            at += len(found.lines)


def _entries(
    text: str, headings: Headings | None = None
) -> Iterator[tuple[Bucket, str, str, bool]]:
    """``(bucket, filename, note, listed)`` for every Example-models entry in
    ``text``; ``listed`` when it is in a sub-bullet."""
    for bucket, _, found in _fields(text.split("\n"), headings):
        examples = register._examples(found)
        for entry in examples.entries:
            yield bucket, entry.model, entry.note, examples.runs[entry.run].bullet


def _prose(text: str, headings: Headings | None = None) -> dict[Bucket, Counter[str]]:
    """Each bucket's Example-models prose (text that belongs to no entry),
    whitespace normalised, with how often each piece occurs."""
    out: dict[Bucket, Counter[str]] = defaultdict(Counter)
    for bucket, _, found in _fields(text.split("\n"), headings):
        out[bucket].update(register._norm([p]) for p in register._examples(found).prose)
    return out


def _contains(longer: str, shorter: str) -> bool:
    """True when note ``shorter`` says nothing ``longer`` does not, in either form."""
    return register._within(register._bare(shorter), register._bare(longer))


def _same(one: str | None, other: str | None) -> bool:
    """Two notes that say the same, whatever form each is written in."""
    if one is None or other is None:
        return one is other
    return register._norm([register._bare(one)]) == register._norm([register._bare(other)])


def _longer(current: str, other: str) -> str:
    """The note that says more, ``current`` on a tie; the form does not count."""
    return other if len(register._bare(other)) > len(register._bare(current)) else current


@dataclass
class Union:
    """Per bucket, the base's entries and those the merged branches changed."""

    annotations: dict[Bucket, dict[str, str]] = field(default_factory=dict)
    """The longest note seen for each filename."""
    listed: dict[Bucket, dict[str, bool]] = field(default_factory=dict)
    """Whether that note was in a sub-bullet."""
    seen: dict[Bucket, dict[str, list[str]]] = field(default_factory=dict)
    """Every other note seen for each filename, for what the longest discards."""
    branch_order: dict[Bucket, list[str]] = field(default_factory=dict)
    base_order: dict[Bucket, list[str]] = field(default_factory=dict)
    corrected: dict[Bucket, dict[str, str]] = field(default_factory=dict)
    """Notes a branch changed from the one it inherited, which the base still
    has unchanged; these win over the longest."""
    superseded: dict[Bucket, dict[str, str]] = field(default_factory=dict)
    """The inherited note each correction replaces: replaced, not discarded."""
    removed: dict[Bucket, set[str]] = field(default_factory=dict)
    """Base entries a branch removed from the bucket."""
    renamed: dict[Bucket, dict[str, str]] = field(default_factory=dict)
    """A removed entry -> the entry the branch put in its place."""
    prose: dict[Bucket, Counter[str]] = field(default_factory=dict)
    """Prose a branch added to a field, with how many times a branch has it:
    a piece counts as added when the branch has it more often than its fork."""
    notes: list[str] = field(default_factory=list)

    def order(self, bucket: Bucket) -> list[str]:
        """Filenames in the order the branches list them, then the base's."""
        ordered = list(self.branch_order.get(bucket, []))
        ordered += [name for name in self.base_order.get(bucket, []) if name not in ordered]
        return ordered


class Entries(dict[tuple[Bucket, str], str]):
    """``(bucket, filename) -> note``, and each bucket's prose (:attr:`prose`,
    see :func:`_prose`), for telling what a branch added to the copy it forked."""

    prose: dict[Bucket, Counter[str]]


Found = list[tuple[Bucket, str, str, bool]]


def _as_map(found: Found, prose: dict[Bucket, Counter[str]]) -> Entries:
    entries = Entries()
    for bucket, filename, note, _ in found:
        entries[(bucket, filename)] = _longer(entries.get((bucket, filename), ""), note)
    entries.prose = prose
    return entries


def entry_map(text: str, headings: Headings | None = None) -> Entries:
    """``(bucket, filename) -> note`` for ``text``, the longest on a repeat."""
    return _as_map(list(_entries(text, headings)), _prose(text, headings))


def _fold(
    found: Found, union: Union, order: dict[Bucket, list[str]], inherited: Entries | None = None
) -> None:
    for bucket, filename, note, listed in found:
        if inherited is not None and _same(inherited.get((bucket, filename)), note):
            continue
        known = union.annotations.setdefault(bucket, {})
        if filename not in known or len(register._bare(note)) > len(
            register._bare(known[filename])
        ):
            known[filename] = note
            union.listed.setdefault(bucket, {})[filename] = listed
        union.seen.setdefault(bucket, {}).setdefault(filename, []).append(note)
        names = order.setdefault(bucket, [])
        if filename not in names:
            names.append(filename)


def _label(bucket: Bucket, filename: str = "") -> str:
    return " / ".join([part for part in bucket if part] + ([filename] if filename else []))


def collect_entries(
    base_text: str,
    branch_copies: Iterable[tuple[str, Entries]],
    headings: Headings | None = None,
) -> Union:
    """Fold the base and then what each branch changed into one :class:`Union`.

    ``branch_copies`` yields each branch's copy of the file with the entries
    at the branch's fork point. An entry already there, note and all, is
    main's content rather than the branch's, and main may have renamed or
    removed it since: on 2026-09-29 the pre-rename
    ``Willmann_2018_rivaroxaban.R`` came back twice, from branches cut before
    the rename. So only entries a branch added, or whose note it changed,
    count.

    Across versions, the LONGEST observed note per ``(cov, sub, fname)``
    wins, the base's on a tie; every other note seen is kept for the report
    of what that discards. ``branch_copies`` is consumed one copy at a time,
    so a large register is never held once per branch. With ``headings``
    (the edited file's, from :func:`headings_of`), every copy's fields are
    bucketed under that file's headings; the fork points' entries in
    ``branch_copies`` must be read with the same ``headings``.

    A branch's removals and corrections count only where the base still has
    what the branch inherited; when main changed the entry too, the branch's
    view is stale and the base's stands. The first branch to correct an
    entry wins; a second that disagrees is noted.
    """
    union = Union()
    base_found = list(_entries(base_text, headings))
    base = _as_map(base_found, {})
    _fold(base_found, union, union.base_order)
    kept_by_branch: set[tuple[Bucket, str]] = set()
    for text, inherited in branch_copies:
        found = list(_entries(text, headings))
        _fold(found, union, union.branch_order, inherited)
        mine = _as_map(found, {})
        kept_by_branch.update(k for k in mine if k not in inherited)
        forked = getattr(inherited, "prose", {})
        for bucket, pieces in _prose(text, headings).items():
            added = pieces - forked.get(bucket, Counter())
            if added:
                union.prose.setdefault(bucket, Counter())
                union.prose[bucket] |= Counter({p: pieces[p] for p in added})
        for entry, note in mine.items():
            was = inherited.get(entry)
            if was is None or _same(was, note) or not _same(base.get(entry), was):
                continue
            bucket, filename = entry
            fixes = union.corrected.setdefault(bucket, {})
            if filename in fixes and fixes[filename] != note:
                union.notes.append(
                    f"# two merged branches correct {_label(bucket, filename)} differently;"
                    " the first is kept"
                )
                continue
            fixes[filename] = note
            union.superseded.setdefault(bucket, {})[filename] = was
        # A removal counts only where the branch still has the heading: when
        # its copy has no entries under it at all, the heading was renamed or
        # the field rewritten, and its entries did not go anywhere.
        my_buckets = {bucket for bucket, _ in mine}
        for entry, was in inherited.items():
            if entry in mine or not _same(base.get(entry), was):
                continue
            if entry[0] in my_buckets:
                union.removed.setdefault(entry[0], set()).add(entry[1])
            else:
                union.notes.append(
                    f"# kept {_label(*entry)}: a merged branch has no entries under that"
                    " heading (renamed or rewritten), so its absence is not a removal"
                )
        # A removed entry the branch replaced where it stood (a rename) keeps
        # its place: align the bucket's names at the fork and in the branch.
        before: dict[Bucket, list[str]] = defaultdict(list)
        after: dict[Bucket, list[str]] = defaultdict(list)
        for b, f in inherited:
            before[b].append(f)
        for b, f in mine:
            after[b].append(f)
        for bucket in before.keys() & after.keys():
            old, new = before[bucket], after[bucket]
            matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
            for tag, i1, i2, j1, j2 in matcher.get_opcodes():
                if tag == "replace":
                    for was_name, now_name in zip(old[i1:i2], new[j1:j2], strict=False):
                        union.renamed.setdefault(bucket, {}).setdefault(was_name, now_name)
    # An entry one branch removed and another added in the same bucket stays.
    for bucket, names in union.removed.items():
        names.difference_update(f for f in list(names) if (bucket, f) in kept_by_branch)
    return union


def _written(filename: str, note: str) -> str:
    return f"`{filename}` {note}" if note else f"`{filename}`"


def _rewrite(found: register._Field, run: register._Run, text: str) -> None:
    """Make ``run``'s text ``text``, rewriting only the part that differs, so
    a wrapped run keeps its line breaks outside it."""
    old = run.text
    if old == text:
        return
    prefix = 0
    while prefix < min(len(old), len(text)) and old[prefix] == text[prefix]:
        prefix += 1
    suffix = 0
    while (
        suffix < min(len(old), len(text)) - prefix
        and old[len(old) - 1 - suffix] == text[len(text) - 1 - suffix]
    ):
        suffix += 1
    register._replace(found, run, prefix, len(old) - suffix, text[prefix : len(text) - suffix])


def _rebuilt(run: register._Run, kept: list[tuple[register._Entry, str | None]]) -> str:
    """``run``'s text with each entry's text replaced: by the text given, or
    gone when that is None. What precedes the first entry and follows the
    last stays; what joins two entries stays while an entry follows it, and
    whatever it holds besides separators stays regardless."""
    text = run.text
    out = text[: kept[0][0].start]
    for n, (entry, new) in enumerate(kept):
        if new is not None:
            out += new
        joint = text[entry.stop : kept[n + 1][0].start] if n + 1 < len(kept) else ""
        later = any(other is not None for _, other in kept[n + 1 :])
        if register._prose(joint) or (new is not None and later):
            # Prose that now opens the run sheds the separator before it.
            out += joint if out.strip() else register.LEADING.sub("", joint)
    return out + text[kept[-1][0].stop :]


@dataclass
class _Emitted:
    text: str
    notes: list[str]
    hand: list[register.HandUnion]


def emit_merged(current_text: str, union: Union) -> _Emitted:
    """Edit each Example-models field in ``current_text`` the union changes.

    A field is read whole, by the dedup's parser, and each entry is its
    filename and note as the parser defines them, wherever it stands. Its
    own entries keep their places and their text; one a merged branch
    removed goes, or is replaced where it stood by the entry the branch
    renamed it to; one whose note the union lengthened or a branch corrected
    gets that note, inline in parentheses. Entries the union adds go after
    the field's last entry, in its form. Prose before, between and after the
    entries stays, and a model listed twice stays listed twice. Notes are
    compared by what they say, not their form, so a second run changes
    nothing.

    Returned as hand unions, beside what the field keeps: every note
    "longest wins" sets aside, prose a branch added that the field lacks,
    and the models a branch added to a field that lists none. Lines are
    split on "\\n" only, and rejoined the same way.
    """
    lines = current_text.split("\n")
    notes = list(union.notes)
    dropped: list[str] = []
    emptied: list[str] = []
    left_alone: list[str] = []
    hand: dict[str, list[register.Difference]] = defaultdict(list)
    changes: list[tuple[int, int, list[str]]] = []
    have: dict[Bucket, tuple[Counter[str], list[str]]] = {}
    """Per bucket, the prose its fields hold after the edits, and the first's lines."""
    for bucket, start, found in _fields(lines):
        known = union.annotations.get(bucket)
        if not known:
            have.setdefault(bucket, (Counter(), found.lines))[0].update(
                register._norm([p]) for p in register._examples(found).prose
            )
            continue
        examples = register._examples(found)
        if not examples.entries:
            have.setdefault(bucket, (Counter(), found.lines))[0].update(
                register._norm([p]) for p in examples.prose
            )
            left_alone.append(_label(bucket))
            for name in union.branch_order.get(bucket, []):
                hand[_label(bucket)].append(
                    register.Difference(
                        f"{register.EXAMPLES} entry for {name}",
                        found.lines,
                        [_written(name, known.get(name, ""))],
                    )
                )
            continue
        own = [e.model for e in examples.entries]
        gone = union.removed.get(bucket, set())
        fixes = union.corrected.get(bucket, {})
        swaps = union.renamed.get(bucket, {})
        seen = union.seen.get(bucket, {})
        replaced = union.superseded.get(bucket, {})
        kept_notes: dict[str, list[str]] = defaultdict(list)
        placed: list[str] = []
        per_run: dict[int, list[tuple[register._Entry, str | None]]] = defaultdict(list)
        for entry in examples.entries:
            name = entry.model
            new: str | None = entry.text
            inline = not examples.runs[entry.run].bullet
            if name in gone:
                new = None
                target = swaps.get(name)
                if target and target not in own and target not in placed:
                    new = _written(target, known.get(target, ""))
                    new = register._as_inline(new) if inline else new
                    placed.append(target)
                dropped.append(_label(bucket, name))
            elif own.count(name) > 1:
                # Listed twice: both stay as written.
                kept_notes[name].append(entry.note)
                placed.append(name)
            else:
                final = fixes[name] if name in fixes else _longer(entry.note, known.get(name, ""))
                if not _same(final, entry.note):
                    new = _written(name, final)
                    new = register._as_inline(new) if inline else new
                kept_notes[name] = [final]
                placed.append(name)
            per_run[entry.run].append((entry, new))
        additions = [n for n in union.order(bucket) if n not in placed and n not in gone]
        for name in additions:
            kept_notes[name] = [known.get(name, "")]
        for name, finals in kept_notes.items():
            for other in dict.fromkeys(seen.get(name, [])):
                if (
                    register._bare(other)
                    and not _same(other, replaced.get(name))
                    and not any(_contains(f, other) for f in finals)
                ):
                    hand[_label(bucket)].append(
                        register.Difference(
                            f"{register.EXAMPLES} entry for {name}",
                            [_written(name, f) for f in finals],
                            [_written(name, other)],
                        )
                    )
        if not placed and not additions:
            have.setdefault(bucket, (Counter(), found.lines))[0].update(
                register._norm([p]) for p in examples.prose
            )
            emptied.append(_label(bucket))
            continue
        work = register._Field(found.name, list(found.lines))
        for run_index in sorted(per_run, reverse=True):
            run = examples.runs[run_index]
            kept = per_run[run_index]
            if run.bullet and all(new is None for _, new in kept):
                first, last = run.spans[0][0], run.spans[-1][0]
                del work.lines[first : last + 1]
            else:
                _rewrite(work, run, _rebuilt(run, kept))
        if additions:
            texts = [
                (_written(n, known.get(n, "")), union.listed.get(bucket, {}).get(n, False))
                for n in additions
            ]
            register._insert_entries(work, texts, register._eol(work.lines[0]))
        have.setdefault(bucket, (Counter(), work.lines))[0].update(
            register._norm([p]) for p in register._examples(work).prose
        )
        if work.lines != found.lines:
            changes.append((start, start + len(found.lines), work.lines))
    for bucket, wanted in union.prose.items():
        held, field_lines = have.get(bucket, (Counter(), []))
        for piece, need in wanted.items():
            if field_lines and held[piece] < need:
                hand[_label(bucket)].append(
                    register.Difference(f"{register.EXAMPLES} prose", field_lines, [piece])
                )
    for first, end, new_lines in sorted(changes, reverse=True):
        lines[first:end] = new_lines
    if dropped:
        notes.append(
            f"# dropped {len(dropped)} entr{'y' if len(dropped) == 1 else 'ies'} a merged branch"
            f" removed: {'; '.join(dropped)}"
        )
    if emptied:
        notes.append(
            f"# left {len(emptied)} Example-models field(s) as they are: merged branches removed"
            f" every entry in them, so check them by hand: {'; '.join(emptied)}"
        )
    if left_alone:
        notes.append(
            f"# left {len(left_alone)} Example-models field(s) that list no model as they are"
            f" (prose); a branch's models for them are in the hand-union report:"
            f" {'; '.join(left_alone)}"
        )
    unions = [register.HandUnion(name, [], diffs) for name, diffs in hand.items()]
    if unions:
        notes.append(
            f"# set aside {sum(len(h.differences) for h in unions)} note(s), prose or model(s)"
            " the field does not keep; they are in the hand-union report"
        )
    return _Emitted("\n".join(lines), notes, unions)


def _branch_copies(
    repo: Path, members: Iterable[merge_set.Member], file_rel: str, headings: Headings
) -> Iterator[tuple[str, Entries]]:
    """Each member's copy at its merged commit, with the entries at its fork
    point, bucketed under ``headings``.

    A member that deleted the file has no copy. Fork points repeat across
    members, so each one's entries are read once.
    """
    at_fork: dict[str, Entries] = {}
    for member in members:
        text = merge_set.read_file(repo, member.merged, file_rel)
        if text is None:
            continue
        if member.fork not in at_fork:
            fork_text = merge_set.read_file(repo, member.fork, file_rel) or ""
            at_fork[member.fork] = entry_map(fork_text, headings)
        yield text, at_fork[member.fork]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--repo", type=Path, required=True, help="Repo path (contains .git and the worktree)."
    )
    ap.add_argument(
        "--branch",
        required=True,
        help="The merge branch name (worktree at <repo>/.worktrees/<branch>).",
    )
    ap.add_argument("--base", default="origin/main", help="Base ref (default: origin/main).")
    ap.add_argument(
        "--pattern",
        default="origin/claude/*",
        help="Source branch refspec under refs/remotes/ (default: origin/claude/*).",
    )
    ap.add_argument(
        "--file", required=True, help="Repo-relative path to the union-merge target file."
    )
    ap.add_argument(
        "--report",
        metavar="FILE",
        help="append each note the union sets aside, beside the one it keeps, to FILE for a "
        "hand union",
    )
    ap.add_argument(
        "--extra-ref",
        action="append",
        default=[],
        help=(
            "Additional fully-qualified refs to include in the union "
            "(e.g. origin/add-Fiedler-Kelly_2019_fremanezumab). Repeat "
            "for multiple. Useful when the bulk merge is one pattern "
            "plus a few hand-picked feature branches."
        ),
    )
    args = ap.parse_args(argv)

    repo: Path = args.repo
    if not repo.is_dir():
        return fail(f"--repo {repo} is not a directory")
    refs = [("--base", args.base), ("--branch", args.branch)]
    refs += [("--extra-ref", ref) for ref in args.extra_ref if ref]
    for flag, ref in refs:
        if not merge_set.resolves(repo, ref):
            return fail(f"{flag} {ref!r} does not resolve to a commit in {repo}")
    worktree = repo / ".worktrees" / args.branch
    if not worktree.is_dir():
        return fail(f"no worktree for --branch {args.branch!r} at {worktree}")
    # A pattern that matches nothing would "reconstruct" the file from the base
    # alone and report success.
    candidates = merge_set.candidate_refs(repo, args.pattern, args.extra_ref)
    if not candidates:
        return fail(f"no branch matches --pattern {args.pattern!r} and no --extra-ref was given")
    target = worktree / args.file
    if not target.exists():
        sys.stderr.write(f"# target file not present on branch: {target}\n")
        return 0  # nothing to do

    found = merge_set.compute(repo, args.branch, args.base, candidates)
    for line in found.summary():
        sys.stderr.write(f"# {line}\n")
    if not found.members:
        return fail(merge_set.empty_message(args.pattern, args.branch, args.base))
    touching = [m for m in found.members if merge_set.touches(repo, m, args.file)]
    sys.stderr.write(f"# branches touching {args.file}: {len(touching)}\n")
    for member in touching:
        sys.stderr.write(f"#   - {member.ref}\n")
    if not touching:
        sys.stderr.write("# no branches touched the union-file; nothing to reconstruct.\n")
        return 0

    with target.open(encoding="utf-8", newline="") as fh:
        current_text = fh.read()
    headings = headings_of(current_text)
    base_text = merge_set.read_file(repo, args.base, args.file) or ""
    union = collect_entries(
        base_text, _branch_copies(repo, touching, args.file, headings), headings
    )
    sys.stderr.write(f"# (cov, sub) buckets with entries: {len(union.annotations)}\n")
    sys.stderr.write(
        f"# total filename entries:           {sum(len(v) for v in union.annotations.values())}\n"
    )

    emitted = emit_merged(current_text, union)
    for note in emitted.notes:
        sys.stderr.write(f"{note}\n")
    if emitted.hand and args.report is not None:
        register.append_report(
            args.report, args.file, emitted.hand, UNION_INTRO, "In a merged copy:"
        )
    elif emitted.hand:
        for h in emitted.hand:
            for d in h.differences:
                sys.stderr.write(f"#   {h.name} / {d.what}: kept {d.kept}; set aside {d.dropped}\n")
    new_text = emitted.text
    if new_text == current_text:
        sys.stderr.write(f"# nothing to add; left unchanged: {target}\n")
        return 0
    with target.open("w", encoding="utf-8", newline="") as fh:
        fh.write(new_text)
    sys.stderr.write(f"# wrote merged file: {target}\n")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception:
        traceback.print_exc()
        raise SystemExit(2) from None
