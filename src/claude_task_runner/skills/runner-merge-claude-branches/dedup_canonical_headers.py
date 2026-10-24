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

* every model a dropped copy lists as an ``- **Example models:**`` entry
  that is no entry of the keeper's, as written, with its annotation (a
  bare filename when it has none). An entry starts at every backticked
  ``*.R`` filename outside parentheses, whatever joins it to the one
  before (``, ``, ``; ``, ``and``, prose); a filename inside an entry's
  parentheses is part of that entry's annotation, and does not count as
  an entry of its own. Entries go in the keeper's form: right after its
  last inline entry (prose after that entry stays last), or as
  sub-bullets under a list-style, empty or ``none`` line;
* their ``- **Source aliases:**`` sub-bullets the keeper lacks.

Everything else a dropped copy says differently is prose that cannot be
unioned mechanically: its heading, Description, Role, Notes, Units, ...,
an Example-models entry for a model the keeper also lists but annotated
differently, and Example-models prose outside any entry. Fields are
compared as written, a repeated field with every field of that name, and
whitespace does not count. A group with such differences is left whole
and reported on stderr for a hand union, so the ``--check`` gate still
sees the duplicate; with ``--report FILE`` it is collapsed like the rest
and what the dropped copies said is appended to FILE, verbatim, beside
what the register keeps. Nothing goes into the register that its copies
did not say. ``##`` section headers are never touched.

Modes:
  (default)   fix in place; print what was collapsed; exit 0, or 3 when a
              group was left for a hand union (the rest are still fixed).
  --report F  as the default, but collapse those groups too and append
              their differences to F; exit 0.
  --check     report duplicates and exit 1 if any remain (a merge gate);
              exit 0 when clean. Makes no edits.
Either mode exits 2, before touching any file, when a named file does not
exist: a gate must not pass on a path it never read.

Usage:
    python3 dedup_canonical_headers.py <file.md> [<file2.md> ...]
    python3 dedup_canonical_headers.py --report hand-union.md <file.md> [...]
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
SUBITEM = re.compile(r"^\s+[-*] ")
MODEL = re.compile(r"`([A-Za-z0-9_.\-]+\.R)`")
EXAMPLES = "Example models"
ALIASES = "Source aliases"
NONE_TEXT = frozenset({"none", "none."})
"""What a field says when it lists nothing."""
CONNECTOR = re.compile(r"(?:[\s,;:/.]|\b(?:and|or|plus)\b)+$")
"""What joins one example entry to the next, stripped from an entry's end."""


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

    @property
    def empty(self) -> bool:
        """True when the field lists nothing: no text after the label (or
        ``none``) and no sub-bullets."""
        return (not self.rest or self.rest.lower() in NONE_TEXT) and not self.items()

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

    def named(self, name: str) -> list[_Field]:
        """Every field of ``name``, in order: a block may repeat one."""
        return [f for f in self.fields if f.name == name]

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


@dataclass
class _Entry:
    """One model in an ``Example models`` field."""

    model: str
    text: str
    """Its text as written: the backticked filename through everything up
    to the next entry, without the separator before that one."""
    end: int
    """Where in the field's text the entry proper ends: after the filename
    and the parenthesised annotation that directly follows it, if any."""


def _closing_paren(text: str, start: int) -> int:
    """The index after the ``)`` that closes the ``(`` at ``start``, outside
    code spans; ``start`` itself when it is unclosed."""
    depth = 0
    in_code = False
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "`":
            in_code = not in_code
        elif not in_code and ch == "(":
            depth += 1
        elif not in_code and ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
    return start


def _split_entries(text: str) -> tuple[str, list[_Entry]]:
    """The prose before the first entry, and the entries. An entry starts at
    every backticked ``*.R`` filename outside parentheses, whatever comes
    before it (``, ``, ``; ``, ``and`` or prose); a filename inside an
    entry's parentheses is a mention within its annotation."""
    starts: list[int] = []
    depth = 0
    in_code = False
    for i, ch in enumerate(text):
        if ch == "`":
            if not in_code and depth == 0 and MODEL.match(text, i):
                starts.append(i)
            in_code = not in_code
        elif not in_code and ch == "(":
            depth += 1
        elif not in_code and ch == ")":
            depth = max(0, depth - 1)
    entries = []
    for n, start in enumerate(starts):
        stop = starts[n + 1] if n + 1 < len(starts) else len(text)
        match = MODEL.match(text, start)
        assert match is not None
        end = match.end()
        after = len(text[end:stop]) - len(text[end:stop].lstrip())
        if text[end + after : end + after + 1] == "(":
            closed = _closing_paren(text, end + after)
            if closed > end + after:
                end = closed
        raw = CONNECTOR.sub("", text[start:stop])
        entries.append(_Entry(match.group(1), raw, end))
    lead = CONNECTOR.sub("", text[: starts[0]] if starts else text)
    return lead, entries


@dataclass
class _Examples:
    """An ``Example models`` field read as entries."""

    prose: list[str]
    """Text in the field that belongs to no entry."""
    entries: list[_Entry]
    inline: bool


def _examples(field: _Field) -> _Examples:
    if field.rest and field.rest.lower() not in NONE_TEXT:
        lead, entries = _split_entries(field.rest)
        return _Examples([lead] if lead else [], entries, True)
    prose: list[str] = []
    entries = []
    for item in field.items():
        text = " ".join(ln.strip() for ln in item)
        text = text[2:] if text[:2] in ("- ", "* ") else text
        lead, found = _split_entries(text)
        if lead:
            prose.append(lead)
        entries += found
    return _Examples(prose, entries, False)


def _eol(line: str) -> str:
    """The carriage return ``line`` ends with in a CRLF file, for the lines
    written beside it."""
    return "\r" if line.endswith("\r") else ""


def _norm(lines: list[str]) -> str:
    """``lines`` with every run of whitespace, tabs and carriage returns
    included, made one space, and blank lines dropped."""
    return " ".join(" ".join(ln.split()) for ln in lines if ln.strip())


def _as_inline(entry: str) -> str:
    """A list-style entry as an inline one: the model, then any text after it
    in parentheses (``-- text`` becomes ``(text)``)."""
    match = MODEL.match(entry)
    if match is None:
        return entry
    rest = entry[match.end() :].strip()
    if not rest:
        return match.group(0)
    if rest.startswith("(") and _closing_paren(rest, 0) == len(rest):
        return f"{match.group(0)} {rest}"
    rest = rest.removeprefix("--").removeprefix(":").strip()
    return f"{match.group(0)} ({rest})"


@dataclass
class Difference:
    """Something a dropped copy says that the kept copy does not."""

    what: str
    kept: list[str]
    dropped: list[str]


def _differences(keeper: _Block, other: _Block) -> list[Difference]:
    """What dropping ``other`` loses outside example models and source
    aliases: a different heading or opening text, and each field it has
    that the keeper lacks or words differently. A field is compared with
    every field of the same name in the keeper, so a block that repeats one
    compares as written. Whitespace does not count."""
    out = []
    if _norm([keeper.heading]) != _norm([other.heading]):
        out.append(Difference("heading", [keeper.heading], [other.heading]))
    if _norm(other.preamble) and _norm(keeper.preamble) != _norm(other.preamble):
        out.append(Difference("text before the first field", keeper.preamble, other.preamble))
    for theirs in other.fields:
        if theirs.name == EXAMPLES:
            continue
        if theirs.name == ALIASES and (theirs.empty or not theirs.rest):
            continue
        mine = keeper.named(theirs.name)
        if _norm(theirs.lines) not in {_norm(f.lines) for f in mine}:
            out.append(Difference(theirs.name, [ln for f in mine for ln in f.lines], theirs.lines))
    return out


def _carry_examples(keeper: _Block, other: _Block) -> tuple[list[str], list[Difference]]:
    """Add ``other``'s example entries whose model is no entry of the
    keeper's, each as written; return the models added, and the entries and
    prose of ``other``'s that the keeper words differently, for a hand
    union. Every model ``other`` lists as an entry is then an entry of the
    keeper's, and none of ``other``'s text is lost."""
    mine_fields = keeper.named(EXAMPLES)
    mine = [_examples(f) for f in mine_fields]
    kept: dict[str, list[_Entry]] = defaultdict(list)
    for ex in mine:
        for e in ex.entries:
            kept[e.model].append(e)
    kept_lines = [ln for f in mine_fields for ln in f.lines]
    kept_text = _norm(kept_lines)
    new: list[_Entry] = []
    from_list: list[bool] = []
    differences: list[Difference] = []
    for field in other.named(EXAMPLES):
        theirs = _examples(field)
        for prose in theirs.prose:
            if _norm([prose]) not in kept_text:
                differences.append(Difference(EXAMPLES, kept_lines, [prose]))
        for e in theirs.entries:
            if e.model in kept:
                # Nothing is lost when the keeper's entry already says it all
                # (a bare filename, or the same annotation).
                if not any(_norm([e.text]) in _norm([k.text]) for k in kept[e.model]):
                    differences.append(
                        Difference(
                            f"{EXAMPLES} entry for {e.model}",
                            [k.text for k in kept[e.model]],
                            [e.text],
                        )
                    )
                continue
            kept[e.model].append(e)
            new.append(e)
            from_list.append(not theirs.inline)
    if not new:
        return [], differences
    eol = _eol(keeper.heading)
    target = next((f for f, ex in zip(mine_fields, mine, strict=True) if ex.inline), None)
    if target is not None:
        rest = target.rest
        at = _insertion_point(rest, _examples(target).entries)
        added = ", ".join(
            _as_inline(e.text) if listed else e.text
            for e, listed in zip(new, from_list, strict=True)
        )
        target.lines[0] = f"- **{EXAMPLES}:** {rest[:at]}, {added}{rest[at:]}{eol}"
    elif mine_fields:
        target = mine_fields[0]
        target.lines[0] = f"- **{EXAMPLES}:**{eol}"
        last = max((i for i, ln in enumerate(target.lines) if ln.strip()), default=0)
        target.lines[last + 1 : last + 1] = [f"  - {e.text}{eol}" for e in new]
    else:
        inline = ", ".join(_as_inline(e.text) for e in new)
        keeper.fields.append(_Field(EXAMPLES, [f"- **{EXAMPLES}:** {inline}.{eol}"]))
    return [e.model for e in new], differences


def _insertion_point(rest: str, entries: list[_Entry]) -> int:
    """Where in an inline ``Example models`` text new entries go: right after
    the last entry, so prose that follows it (a closing remark after its
    full stop) stays last; at the end, before a final full stop, when the
    last entry runs on (``-- text``) or there is none."""
    end = len(rest) - 1 if rest.endswith(".") else len(rest)
    if not entries:
        return end
    after = rest[entries[-1].end :]
    if not after.strip() or after.strip() == "." or after.lstrip()[:1] in (".", ";"):
        return entries[-1].end
    return end


def _carry_aliases(keeper: _Block, other: _Block) -> int:
    """Add ``other``'s source-alias sub-bullets the keeper lacks, giving the
    keeper the field, before its example models, when it has none or says
    ``none``; return how many were added."""
    added = 0
    for theirs in other.named(ALIASES):
        if theirs.rest:
            continue
        mine = keeper.named(ALIASES)
        target = mine[0] if mine else None
        if target is None:
            target = _Field(ALIASES, [theirs.lines[0]])
            at = next((i for i, f in enumerate(keeper.fields) if f.name == EXAMPLES), None)
            keeper.fields.insert(len(keeper.fields) if at is None else at, target)
        elif target.empty:
            target.lines[:] = [f"- **{ALIASES}:**{_eol(target.lines[0])}"]
        have = {_norm(item) for f in keeper.named(ALIASES) for item in f.items()}
        new = [item for item in theirs.items() if _norm(item) not in have]
        if new:
            last = max((i for i, ln in enumerate(target.lines) if ln.strip()), default=0)
            target.lines[last + 1 : last + 1] = [ln for item in new for ln in item]
            added += len(new)
    return added


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
    differences: list[Difference]

    @property
    def fields(self) -> list[str]:
        return list(dict.fromkeys(d.what for d in self.differences))


def dedup(
    lines: list[str], global_unique: bool = False, *, collapse_differing: bool = False
) -> tuple[list[str], list[Collapsed], list[HandUnion]]:
    """Collapse duplicate ### blocks. Returns the new lines, the groups
    collapsed, and the groups whose dropped copies say something the kept
    copy does not (see :func:`_differences` and :func:`_carry_examples`).
    Those are left whole unless ``collapse_differing``; then they are
    collapsed too, and what the dropped copies said is only in the
    returned :class:`HandUnion` records, for the caller to keep."""
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
        differences = [d for o in others for d in _differences(keeper, o)]
        examples: list[str] = []
        aliases = 0
        for other in others:
            models, unmatched = _carry_examples(keeper, other)
            examples += models
            differences += unmatched
            aliases += _carry_aliases(keeper, other)
        if differences:
            hand.append(HandUnion(name, [i + 1 for i in idxs], differences))
            if not collapse_differing:
                continue
        for span in spans:
            replace.append((*span, keeper.lines() if span == keep_span else []))
        collapsed.append(Collapsed(name, len(idxs), examples, aliases))

    ends_with_newline = bool(lines) and lines[-1] == ""
    for s, e, new in sorted(replace, key=lambda r: -r[0]):
        lines[s:e] = new
    # Dropping the file's last block drops the line break that ended it.
    if ends_with_newline and lines and lines[-1] != "":
        if lines[-1].strip():
            lines.append("")
        else:
            lines[-1] = ""
    return lines, collapsed, hand


def _plain(lines: list[str]) -> list[str]:
    return [ln.rstrip("\r") for ln in lines]


def _report(path: str, hand: list[HandUnion]) -> str:
    """The hand-union report for ``path``: per group, each thing a dropped
    copy said that the register now lacks, with what the register says, both
    verbatim."""
    out = [f"# Hand union needed in {path}", ""]
    out += [
        "Each canonical below was registered more than once. The register keeps",
        "one copy (the longest); what a dropped copy said differently is below,",
        "verbatim. Union it into the register by hand, keeping each source's",
        "attribution, and commit.",
        "",
    ]
    fence = "````"
    for h in hand:
        out += [f"## {h.name} (lines {h.lines} before the dedup)", ""]
        for d in h.differences:
            out += [f"### {d.what}", ""]
            if d.kept:
                out += ["In the register:", "", fence, *_plain(d.kept), fence, ""]
            else:
                out += ["In the register: nothing.", ""]
            out += ["In a dropped copy:", "", fence, *_plain(d.dropped), fence, ""]
    return "\n".join(out)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("files", nargs="+")
    ap.add_argument(
        "--check", action="store_true", help="report duplicates and exit 1 if any; make no edits"
    )
    ap.add_argument(
        "--report",
        metavar="FILE",
        help="collapse every group, also those whose copies differ beyond example models "
        "and source aliases, and append what the dropped copies said there to FILE for a "
        "hand union",
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
        # newline="": a CRLF register keeps its line endings.
        with open(path, encoding="utf-8", newline="") as fh:
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
        new_lines, collapsed, hand = dedup(
            lines, args.global_unique, collapse_differing=args.report is not None
        )
        if collapsed:
            with open(path, "w", encoding="utf-8", newline="") as fh:
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
        if hand and args.report is not None:
            with open(args.report, "a", encoding="utf-8") as fh:
                fh.write(_report(path, hand) + "\n")
            for h in hand:
                sys.stderr.write(
                    f"# HAND UNION NEEDED in {path}: ### {h.name}: the copies differ in "
                    f"{', '.join(h.fields)}; collapsed, the dropped wording is in {args.report}\n"
                )
        else:
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
