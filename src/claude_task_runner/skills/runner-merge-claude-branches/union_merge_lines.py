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
3. Parses every ``**Example models:**`` line and buckets the
   ``(filename, annotation)`` entries by ``(covariate header,
   subsection header)`` resolved from the most recent ``##``/``###``
   markdown headings.
4. Builds a union of the base's entries and those each branch changed:
   ones it added, or whose annotation it changed, relative to its fork
   point. An entry a branch only inherited is main's; main may have
   renamed or removed it since. The longest (most informative)
   annotation per filename per bucket wins.
5. Walks the CURRENT (post-merge) file and re-emits each
   Example-models line the union adds to, keeping the line's own prefix
   and the text after its last entry. A line the union adds nothing to is
   left byte-for-byte as it is, so a second run changes nothing.
6. Writes the result back to the merge branch's worktree.

The re-emit used to append a full stop to every line it rebuilt, and to
rebuild every line. Where an annotation opens a "(" it never closes, the
annotation swallowed the line's final full stop, so each consolidation round
added one more: three lines of nlmixr2lib's covariate register reached 13 or
more. Existing runs of full stops are left as they are; they no longer grow.

Out-of-scope additions (brand-new lines, new sections, table rows
that aren't Example-models lines) are NOT touched — the merge
already handles those via standard 3-way merging because they
appear at unique line positions per branch. Neither is an Example-models
line with no inline list: a list-style heading, whose models are sub-bullets
under it, or a prose body such as "none yet".

If the file's structured-markdown shape differs from
"Example models" lines, extend ``EXAMPLE_LINE_RE`` to a list of
patterns and add per-pattern parser functions. Open to PRs.

Exit codes: 0 done (including nothing to reconstruct, or the file is not in the
worktree); 2 it could not run: a bad argument, a ``--base``, ``--branch`` or
``--extra-ref`` that does not resolve, no worktree for ``--branch``, no branch
matching ``--pattern``, an empty merge set, or a failed git command.
"""

from __future__ import annotations

import argparse
import re
import sys
import traceback
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import merge_set

# Regex for the structured line we union-merge. Currently only one
# shape is supported; the regex is permissive on whitespace and bullet
# style (allows ``- ``, ``* `` or numbered) so it picks up most
# reasonable markdown.
EXAMPLE_LINE_RE = re.compile(r"^\s*(?:[-*]|\d+\.)\s*\*\*Example models:\*\*\s*(.*)$")
SECTION_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
FILENAME_RE = re.compile(r"`([^`]+\.R)`")
# Where the next entry starts: a comma, then a backticked model file.
NEXT_ENTRY_RE = re.compile(r",\s*`[^`]+\.R`")

Bucket = tuple[str, str]


def fail(message: str) -> int:
    sys.stderr.write(f"ERROR: (union-merge) {message}\n")
    return 2


def _closing_paren(text: str, start: int) -> int | None:
    """Index of the ")" that closes the "(" at ``start``, or None if none does."""
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index
    return None


def _unclosed_annotation_end(body: str, start: int) -> int:
    """Where an annotation ends whose "(" at ``start`` never closes.

    Scanning for the closing paren alone runs to the end of the body, so the
    annotation swallows the line's final full stop -- and every entry after
    it. It ends instead just before the next entry, else just after its last
    ")", else just before the run of full stops that ends the line.
    """
    following = NEXT_ENTRY_RE.search(body, start)
    if following is not None:
        return following.start()
    last = body.rfind(")", start)
    if last != -1:
        return last + 1
    return start + len(body[start:].rstrip(". \t"))


def split_example_body(body: str) -> tuple[list[tuple[str, str]], str]:
    """Split an Example-models body into its entries and the text after them.

    ``"`A.R` (annot), `B.R`, `C.R` (annot)."`` gives
    ``([("A.R", "(annot)"), ("B.R", ""), ("C.R", "(annot)")], ".")``.

    The second value is everything after the last entry: normally the final
    full stop, sometimes a sentence of prose. Joining the entries with ", "
    and appending it rebuilds the body; what separated the entries is not
    kept. Nested parens inside an annotation are balanced; see
    :func:`_unclosed_annotation_end` for one that is not.
    """
    entries: list[tuple[str, str]] = []
    end = 0
    position = 0
    while (match := FILENAME_RE.search(body, position)) is not None:
        stop = match.end()
        cursor = stop
        while cursor < len(body) and body[cursor] in " \t":
            cursor += 1
        annotation = ""
        if cursor < len(body) and body[cursor] == "(":
            close = _closing_paren(body, cursor)
            stop = close + 1 if close is not None else _unclosed_annotation_end(body, cursor)
            annotation = body[cursor:stop]
        entries.append((match.group(1), annotation))
        end = position = stop
        while position < len(body) and body[position] in ", \t":
            position += 1
    return entries, body[end:]


def parse_example_models(body: str) -> list[tuple[str, str]]:
    """The ``(filename, annotation)`` entries of an Example-models body, in order."""
    return split_example_body(body)[0]


def _sections(lines: Sequence[str]) -> list[Bucket]:
    """``(cov, sub)`` for each line: the latest ``##`` heading and the latest deeper one.

    The bucket key is the pair, so the same subsection name under different
    covariates is kept distinct.
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
        found.append((cov, sub))
    return found


def _longer(current: str, other: str) -> str:
    return other if len(other) > len(current) else current


@dataclass
class Union:
    """Per bucket, the base's entries and those the merged branches changed."""

    annotations: dict[Bucket, dict[str, str]] = field(default_factory=dict)
    """The longest annotation seen for each filename."""
    branch_order: dict[Bucket, list[str]] = field(default_factory=dict)
    base_order: dict[Bucket, list[str]] = field(default_factory=dict)

    def order(self, bucket: Bucket) -> list[str]:
        """Filenames in the order the branches list them, then the base's."""
        ordered = list(self.branch_order.get(bucket, []))
        ordered += [name for name in self.base_order.get(bucket, []) if name not in ordered]
        return ordered


Entries = dict[tuple[Bucket, str], str]


def _entries(text: str) -> Iterator[tuple[Bucket, str, str]]:
    """``(bucket, filename, annotation)`` for every Example-models entry in ``text``."""
    lines = text.splitlines()
    for line, bucket in zip(lines, _sections(lines), strict=True):
        match = EXAMPLE_LINE_RE.match(line)
        if match:
            for filename, annotation in parse_example_models(match.group(1)):
                yield bucket, filename, annotation


def entry_map(text: str) -> Entries:
    """``(bucket, filename) -> annotation`` for ``text``, the longest on a repeat."""
    found: Entries = {}
    for bucket, filename, annotation in _entries(text):
        found[(bucket, filename)] = _longer(found.get((bucket, filename), ""), annotation)
    return found


def _fold(
    text: str, union: Union, order: dict[Bucket, list[str]], inherited: Entries | None = None
) -> None:
    for bucket, filename, annotation in _entries(text):
        if inherited is not None and inherited.get((bucket, filename)) == annotation:
            continue
        known = union.annotations.setdefault(bucket, {})
        known[filename] = _longer(known.get(filename, ""), annotation)
        listed = order.setdefault(bucket, [])
        if filename not in listed:
            listed.append(filename)


def collect_entries(base_text: str, branch_copies: Iterable[tuple[str, Entries]]) -> Union:
    """Fold the base and then what each branch changed into one :class:`Union`.

    ``branch_copies`` yields each branch's copy of the file with the entries
    at the branch's fork point. An entry already there, annotation and all,
    is main's content rather than the branch's, and main may have renamed or
    removed it since: on 2026-09-29 the pre-rename
    ``Willmann_2018_rivaroxaban.R`` came back twice, from branches cut before
    the rename. So only entries a branch added, or whose annotation it
    changed, count.

    Across versions, the LONGEST observed annotation per ``(cov, sub,
    fname)`` wins, the base's on a tie. The reasoning: each branch's
    annotation is its model-specific note; longer non-empty annotations
    are strictly more informative. ``branch_copies`` is consumed one copy
    at a time, so a large register is never held once per branch.
    """
    union = Union()
    _fold(base_text, union, union.base_order)
    for text, inherited in branch_copies:
        _fold(text, union, union.branch_order, inherited)
    return union


def emit_merged(current_text: str, union: Union) -> tuple[str, list[str]]:
    """Rewrite each Example-models line in ``current_text`` the union adds to.

    Returns the new text and notes for the operator. A rebuilt line lists
    its own models first, in their order, then any the branches add in
    branch order, then any from the base; each keeps the longest annotation
    seen, its own included. The prefix and the text after the last entry
    are the line's own. A line whose union adds no model and lengthens no
    annotation is kept exactly, so re-running changes nothing.

    Lines are split on "\\n" only, and rejoined the same way, so every line
    left alone stays byte-for-byte what it was.
    """
    lines = current_text.split("\n")
    out: list[str] = []
    left_alone: list[str] = []
    for line, bucket in zip(lines, _sections(lines), strict=True):
        match = EXAMPLE_LINE_RE.match(line)
        known = union.annotations.get(bucket) if match else None
        if match is None or not known:
            out.append(line)
            continue
        entries, tail = split_example_body(match.group(1))
        if not entries:
            left_alone.append(" / ".join(part for part in bucket if part))
            out.append(line)
            continue
        own: dict[str, str] = {}
        for filename, annotation in entries:
            own[filename] = _longer(own.get(filename, annotation), annotation)
        ordered = list(own) + [name for name in union.order(bucket) if name not in own]
        notes = {name: _longer(own.get(name, ""), known.get(name, "")) for name in ordered}
        if len(ordered) == len(own) and all(notes[name] == own[name] for name in own):
            out.append(line)
            continue
        body = ", ".join(
            f"`{name}` {notes[name]}" if notes[name] else f"`{name}`" for name in ordered
        )
        out.append(line[: match.start(1)] + body + tail)
    report = []
    if left_alone:
        report.append(
            f"# left {len(left_alone)} Example-models line(s) with no inline list of models"
            f" as they are (a list-style heading or prose): {'; '.join(left_alone)}"
        )
    return "\n".join(out), report


def _branch_copies(
    repo: Path, members: Iterable[merge_set.Member], file_rel: str
) -> Iterator[tuple[str, Entries]]:
    """Each member's copy at its merged commit, with the entries at its fork point.

    A member that deleted the file has no copy. Fork points repeat across
    members, so each one's entries are read once.
    """
    at_fork: dict[str, Entries] = {}
    for member in members:
        text = merge_set.read_file(repo, member.merged, file_rel)
        if text is None:
            continue
        if member.fork not in at_fork:
            at_fork[member.fork] = entry_map(merge_set.read_file(repo, member.fork, file_rel) or "")
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

    base_text = merge_set.read_file(repo, args.base, args.file) or ""
    union = collect_entries(base_text, _branch_copies(repo, touching, args.file))
    sys.stderr.write(f"# (cov, sub) buckets with entries: {len(union.annotations)}\n")
    sys.stderr.write(
        f"# total filename entries:           {sum(len(v) for v in union.annotations.values())}\n"
    )

    with target.open(encoding="utf-8", newline="") as fh:
        current_text = fh.read()
    new_text, report = emit_merged(current_text, union)
    for note in report:
        sys.stderr.write(f"{note}\n")
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
