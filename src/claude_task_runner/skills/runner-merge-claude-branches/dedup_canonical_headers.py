#!/usr/bin/env python3
"""Collapse exact-duplicate ``### CANONICAL`` register entries after a merge.

Companion to ``union_merge_lines.py``. That script folds every branch's
``**Example models:**`` annotations back into shared lines; it does NOT
notice when two branches each *added the same brand-new* ``### NAME``
block (both branched from an older main, so each carries the full
addition). The ``-X theirs`` bulk merge then leaves the SAME canonical
registered twice — exact-duplicate H3 headings. ``verify_section_headers.py``
checks new headers *survived*, not that they are *unique*, so the
duplicate slips through to origin/main (8 such pairs were found in
covariate-columns.md on 2026-07-25).

This closes that gap. Two scopes:

* **default (section-scoped)** — a duplicate is only the same ``### NAME``
  appearing more than once *under the same ``##`` section*. Safe for
  compartment-names.md, where the same drug token is legitimately
  registered in different sections (e.g. ``### col`` as a compartment
  AND as a metabolite/drug suffix) — those must NOT be collapsed.
* **--global** — a canonical must be unique across the WHOLE file
  regardless of section. Correct for covariate-columns.md (the union
  file): a covariate has one canonical meaning, so the same name under
  two ``##`` sections is a mis-filed merge artifact. 4 of the 8 dups
  found on 2026-07-25 were cross-section and only ``--global`` catches
  them. The destructive whole-file behaviour is opt-in so it can never
  accidentally collapse compartment-names.md's intentional reuse.

For each real duplicate group it keeps the longest (most complete) block
and deletes the others, after carrying into the keeper what only they have:

* their ``- **Example models:**`` entries whose model the keeper does not
  name, each with its own annotation (an entry that has none is carried as
  the bare filename). Entries go in the keeper's form: appended to an
  inline list, or as sub-bullets under a list-style or empty line;
* their ``- **Source aliases:**`` sub-bullets the keeper lacks.

Every other part of a block (the heading, Description, Role, Notes, Units,
...) is prose that cannot be unioned mechanically, and two copies that
differ there are two definitions of one canonical, which needs a person.
Such a group is left as it is and reported on stderr for a hand union; the
``--check`` gate then still sees the duplicate. ``##`` section headers are
never touched.

Modes:
  (default)   fix in place; print what was collapsed; exit 0, or 3 when a
              group was left for a hand union (the rest are still fixed).
  --check     report duplicates and exit 1 if any remain (a merge gate);
              exit 0 when clean. Makes no edits.
Either mode exits 2, before touching any file, when a named file does not
exist: a gate must not pass on a path it never read.

Usage:
    python3 dedup_canonical_headers.py <file.md> [<file2.md> ...]
    python3 dedup_canonical_headers.py --check <file.md> [...]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass

H3 = re.compile(r"^###\s+(\S+)")
SECT2 = re.compile(r"^##\s+(.+?)\s*$")  # depth-2 section header
ANY_HEAD = re.compile(r"^#{2,6}\s")  # any block boundary (## or ###+)


Key = str | tuple[str, str]


def _parse(lines: list[str]) -> tuple[list[tuple[int, str, str]], list[int]]:
    """Return (heads, bounds) where:
    heads    = list of (line_idx, name, section) for each ### heading
    bounds   = sorted block-boundary indices (## / ### headings + EOF)
    """
    heads: list[tuple[int, str, str]] = []
    bounds: set[int] = set()
    cur_sect = ""
    for i, ln in enumerate(lines):
        s = SECT2.match(ln)
        if s:
            cur_sect = s.group(1)
        if ANY_HEAD.match(ln):
            bounds.add(i)
        m = H3.match(ln)
        if m:
            heads.append((i, m.group(1), cur_sect))
    bounds.add(len(lines))
    return heads, sorted(bounds)


def _block_end(i: int, bounds: list[int]) -> int:
    for b in bounds:
        if b > i:
            return b
    return bounds[-1]


def _key(name: str, sect: str, global_unique: bool) -> Key:
    return name if global_unique else (sect, name)


def find_dups(lines: list[str], global_unique: bool = False) -> dict[Key, list[int]]:
    """Return {key: [line_idx, ...]} for names appearing >1. Key is the
    bare name when ``global_unique`` else ``(section, name)``."""
    heads, _ = _parse(lines)
    groups: dict[Key, list[int]] = defaultdict(list)
    for i, name, sect in heads:
        groups[_key(name, sect, global_unique)].append(i)
    return {k: v for k, v in groups.items() if len(v) > 1}


FIELD = re.compile(r"^- \*\*([^*]+?):\*\*(.*)$")
SUBITEM = re.compile(r"^\s+- ")
MODEL = re.compile(r"`([A-Za-z0-9_.\-]+\.R)`")
EXAMPLES = "Example models"
ALIASES = "Source aliases"


@dataclass
class _Field:
    name: str
    lines: list[str]

    @property
    def rest(self) -> str:
        """The text after the field's label on its first line."""
        match = FIELD.match(self.lines[0])
        assert match is not None
        return match.group(2).strip()

    def items(self) -> list[list[str]]:
        """The field's sub-bullets, each with its continuation lines."""
        out: list[list[str]] = []
        for line in self.lines[1:]:
            if SUBITEM.match(line):
                out.append([line])
            elif out and line.strip():
                out[-1].append(line)
        return out


@dataclass
class _Block:
    heading: str
    preamble: list[str]
    fields: list[_Field]
    tail: list[str]

    def field(self, name: str) -> _Field | None:
        return next((f for f in self.fields if f.name == name), None)

    def lines(self) -> list[str]:
        return [
            self.heading,
            *self.preamble,
            *(ln for f in self.fields for ln in f.lines),
            *self.tail,
        ]


def _parse_block(lines: list[str]) -> _Block:
    """Split a ``### NAME`` block into its heading, the lines before its
    first ``- **Field:**``, its fields and its trailing blank lines."""
    body = lines[1:]
    end = len(body)
    while end > 0 and not body[end - 1].strip():
        end -= 1
    preamble: list[str] = []
    fields: list[_Field] = []
    for line in body[:end]:
        match = FIELD.match(line)
        if match:
            fields.append(_Field(match.group(1).strip(), [line]))
        elif fields:
            fields[-1].lines.append(line)
        else:
            preamble.append(line)
    return _Block(lines[0], preamble, fields, body[end:])


def _inline_entries(text: str) -> list[str]:
    """The entries of an inline ``Example models`` list: each starts at a
    backticked model filename that opens the text or follows ``, `` outside
    parentheses, and runs to the next one; the final full stop is dropped."""
    starts: list[int] = []
    depth = 0
    in_code = False
    for i, ch in enumerate(text):
        if ch == "`":
            if not in_code and depth == 0 and (i == 0 or text[i - 2 : i] == ", "):
                match = MODEL.match(text, i)
                if match:
                    starts.append(i)
            in_code = not in_code
        elif not in_code and ch == "(":
            depth += 1
        elif not in_code and ch == ")":
            depth = max(0, depth - 1)
    entries = []
    for n, start in enumerate(starts):
        stop = starts[n + 1] if n + 1 < len(starts) else len(text)
        entry = text[start:stop].rstrip()
        entry = entry.removesuffix(",") if n + 1 < len(starts) else entry.removesuffix(".")
        entries.append(entry.rstrip())
    return entries


def _example_entries(field: _Field) -> list[str]:
    """Each example entry as written: inline entries, else the sub-bullets'
    text without their bullet, continuation lines joined."""
    if field.rest:
        return _inline_entries(field.rest)
    return [" ".join(ln.strip() for ln in item).removeprefix("- ") for item in field.items()]


def _as_inline(entry: str) -> str:
    """A list-style entry as an inline one: the model, then any text after it
    in parentheses (``-- text.`` becomes ``(text)``)."""
    match = MODEL.match(entry)
    if match is None:
        return entry
    rest = entry[match.end() :].strip().removesuffix(".").strip()
    if rest.startswith("(") and rest.endswith(")"):
        return f"{match.group(0)} {rest}"
    rest = rest.removeprefix("--").removeprefix(":").strip()
    return f"{match.group(0)} ({rest})" if rest else match.group(0)


def _norm(lines: list[str]) -> str:
    return " ".join(" ".join(ln.split()) for ln in lines if ln.strip())


def _differences(keeper: _Block, other: _Block) -> list[str]:
    """What dropping ``other`` would lose: a different heading or opening
    text, and each field it has that the keeper lacks or words differently.
    Example models, and source aliases given as sub-bullets, are unioned
    instead; a field only the keeper has loses nothing."""
    out = []
    if _norm([keeper.heading]) != _norm([other.heading]):
        out.append("heading")
    if _norm(other.preamble) and _norm(keeper.preamble) != _norm(other.preamble):
        out.append("text before the first field")
    for theirs in other.fields:
        if theirs.name == EXAMPLES:
            continue
        mine = keeper.field(theirs.name)
        if theirs.name == ALIASES and not theirs.rest and (mine is None or not mine.rest):
            continue
        if mine is None or _norm(mine.lines) != _norm(theirs.lines):
            out.append(theirs.name)
    return out


def _carry_examples(keeper: _Block, other: _Block) -> list[str]:
    """Add ``other``'s example entries whose model the keeper does not name;
    return the models added."""
    theirs = other.field(EXAMPLES)
    if theirs is None:
        return []
    mine = keeper.field(EXAMPLES)
    named = set(MODEL.findall("\n".join(mine.lines))) if mine is not None else set()
    new: list[str] = []
    models: list[str] = []
    for entry in _example_entries(theirs):
        match = MODEL.match(entry)
        if match is None or match.group(1) in named:
            continue
        named.add(match.group(1))
        new.append(entry)
        models.append(match.group(1))
    if not new:
        return []
    from_list = not theirs.rest
    if mine is None:
        keeper.fields.append(
            _Field(EXAMPLES, [f"- **{EXAMPLES}:** " + ", ".join(_as_inline(e) for e in new) + "."])
        )
    elif mine.rest:
        inline = [_as_inline(e) if from_list else e for e in new]
        first = mine.lines[0].rstrip().removesuffix(".")
        mine.lines[0] = first + ", " + ", ".join(inline) + "."
    else:
        last = max((i for i, ln in enumerate(mine.lines) if ln.strip()), default=0)
        mine.lines[last + 1 : last + 1] = [f"  - {e}" for e in new]
    return models


def _carry_aliases(keeper: _Block, other: _Block) -> int:
    """Add ``other``'s source-alias sub-bullets the keeper lacks, giving the
    keeper the field, before its example models, when it has none; return
    how many were added."""
    theirs = other.field(ALIASES)
    if theirs is None or theirs.rest:
        return 0
    mine = keeper.field(ALIASES)
    if mine is None:
        mine = _Field(ALIASES, [theirs.lines[0]])
        at = next((i for i, f in enumerate(keeper.fields) if f.name == EXAMPLES), None)
        keeper.fields.insert(len(keeper.fields) if at is None else at, mine)
    have = {_norm(item) for item in mine.items()}
    new = [item for item in theirs.items() if _norm(item) not in have]
    if new:
        last = max((i for i, ln in enumerate(mine.lines) if ln.strip()), default=0)
        mine.lines[last + 1 : last + 1] = [ln for item in new for ln in item]
    return len(new)


@dataclass
class Collapsed:
    name: str
    copies: int
    examples: list[str]
    aliases: int


@dataclass
class HandUnion:
    name: str
    lines: list[int]
    fields: list[str]


def dedup(
    lines: list[str], global_unique: bool = False
) -> tuple[list[str], list[Collapsed], list[HandUnion]]:
    """Collapse duplicate ### blocks. Returns the new lines, the groups
    collapsed, and the groups left for a hand union because their copies
    differ beyond example models and source aliases."""
    heads, bounds = _parse(lines)
    groups: dict[Key, list[int]] = defaultdict(list)
    names: dict[Key, str] = {}
    for i, name, sect in heads:
        k = _key(name, sect, global_unique)
        groups[k].append(i)
        names[k] = name

    replace: list[tuple[int, int, list[str]]] = []
    collapsed: list[Collapsed] = []
    hand: list[HandUnion] = []
    for key, idxs in groups.items():
        if len(idxs) < 2:
            continue
        name = names[key]
        spans = [(i, _block_end(i, bounds)) for i in idxs]
        keep_span = max(spans, key=lambda b: sum(len(lines[n]) for n in range(b[0], b[1])))
        keeper = _parse_block(lines[keep_span[0] : keep_span[1]])
        others = [_parse_block(lines[s:e]) for s, e in spans if (s, e) != keep_span]
        differing = list(dict.fromkeys(f for o in others for f in _differences(keeper, o)))
        if differing:
            hand.append(HandUnion(name, [i + 1 for i in idxs], differing))
            continue
        examples: list[str] = []
        aliases = 0
        for other in others:
            examples += _carry_examples(keeper, other)
            aliases += _carry_aliases(keeper, other)
        for span in spans:
            replace.append((*span, keeper.lines() if span == keep_span else []))
        collapsed.append(Collapsed(name, len(idxs), examples, aliases))

    for s, e, new in sorted(replace, key=lambda r: -r[0]):
        lines[s:e] = new
    return lines, collapsed, hand


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("files", nargs="+")
    ap.add_argument(
        "--check", action="store_true", help="report duplicates and exit 1 if any; make no edits"
    )
    ap.add_argument(
        "--global",
        dest="global_unique",
        action="store_true",
        help="treat a canonical as unique across the WHOLE file "
        "(covariate-columns.md); default is per-##-section "
        "(safe for compartment-names.md cross-section reuse)",
    )
    args = ap.parse_args(argv)

    scope = "whole-file" if args.global_unique else "per-##-section"
    missing = [path for path in args.files if not os.path.isfile(path)]
    if missing:
        sys.stderr.write(f"ERROR: (dedup) no such file: {', '.join(missing)}\n")
        return 2
    any_dups = False
    any_hand = False
    for path in args.files:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        lines = text.split("\n")
        if args.check:
            dups = find_dups(lines, args.global_unique)
            if dups:
                any_dups = True
                sys.stderr.write(
                    f"# DUPLICATE canonical headers ({scope}) in {path}: {len(dups)}\n"
                )
                for k, idxs in sorted(dups.items(), key=lambda kv: str(kv[0])):
                    label = k if isinstance(k, str) else f"[{k[0]}] {k[1]}"
                    sys.stderr.write(f"#   {label} x{len(idxs)} (lines {[i + 1 for i in idxs]})\n")
            else:
                sys.stderr.write(f"# clean (no {scope} duplicate canonicals): {path}\n")
            continue
        new_lines, collapsed, hand = dedup(lines, args.global_unique)
        if collapsed:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(new_lines))
            print(f"deduped {len(collapsed)} canonical name(s) in {path}:")
            for c in collapsed:
                carried = []
                if c.examples:
                    carried.append(f"+examples {c.examples}")
                if c.aliases:
                    carried.append(f"+{c.aliases} source alias(es)")
                print(
                    f"  {c.name}: {c.copies} -> 1"
                    + (f"  ({'; '.join(carried)})" if carried else "")
                )
        elif not hand:
            print(f"no duplicate canonicals ({scope}) in {path}")
        for h in hand:
            any_hand = True
            sys.stderr.write(
                f"# HAND UNION NEEDED in {path}: ### {h.name} (lines {h.lines}): the copies "
                f"differ in {', '.join(h.fields)}; left as they are, merge them by hand\n"
            )
    if args.check:
        return 1 if any_dups else 0
    return 3 if any_hand else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
