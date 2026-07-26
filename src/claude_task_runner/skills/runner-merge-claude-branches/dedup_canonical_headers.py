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

For each real duplicate group it keeps the longest (most complete)
block, unions any unique example ``.R`` filenames from the discarded
blocks into the keeper's ``- **Example models:**`` line, and deletes the
redundant blocks. ``##`` section headers are never touched.

Modes:
  (default)   fix in place; print what was collapsed; exit 0.
  --check     report duplicates and exit 1 if any remain (a merge gate);
              exit 0 when clean. Makes no edits.

Usage:
    python3 dedup_canonical_headers.py <file.md> [<file2.md> ...]
    python3 dedup_canonical_headers.py --check <file.md> [...]
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict

H3 = re.compile(r"^###\s+(\S+)")
SECT2 = re.compile(r"^##\s+(.+?)\s*$")  # depth-2 section header
ANY_HEAD = re.compile(r"^#{2,6}\s")  # any block boundary (## or ###+)
EXR = re.compile(r"([A-Za-z0-9_]+\.R)")


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


def dedup(
    lines: list[str], global_unique: bool = False
) -> tuple[list[str], list[tuple[str, int, list[str]]]]:
    """Collapse duplicate ### blocks in place. Returns
    (new_lines, collapsed) where collapsed is a list of (name, n, extra)."""
    heads, bounds = _parse(lines)
    groups: dict[Key, list[int]] = defaultdict(list)
    names: dict[Key, str] = {}
    for i, name, sect in heads:
        k = _key(name, sect, global_unique)
        groups[k].append(i)
        names[k] = name

    to_delete: list[tuple[int, int]] = []
    collapsed: list[tuple[str, int, list[str]]] = []
    for key, idxs in groups.items():
        if len(idxs) < 2:
            continue
        name = names[key]
        blocks = [(i, _block_end(i, bounds)) for i in idxs]
        keeper = max(blocks, key=lambda b: sum(len(lines[n]) for n in range(b[0], b[1])))
        keep_ex = set(EXR.findall("\n".join(lines[keeper[0] : keeper[1]])))
        extra: list[str] = []
        for b in blocks:
            if b == keeper:
                continue
            for r in EXR.findall("\n".join(lines[b[0] : b[1]])):
                if r not in keep_ex:
                    keep_ex.add(r)
                    extra.append(r)
            to_delete.append(b)
        if extra:
            for j in range(keeper[0], keeper[1]):
                if lines[j].lstrip().startswith("- **Example models:**"):
                    t = lines[j].rstrip()
                    if t.endswith("."):
                        t = t[:-1]
                    lines[j] = (
                        t
                        + ", "
                        + ", ".join(f"`{r}`" for r in extra)
                        + " (merged from a duplicate register entry during merge dedup)."
                    )
                    break
        collapsed.append((name, len(idxs), extra))

    for s, e in sorted(to_delete, key=lambda b: -b[0]):
        del lines[s:e]
    return lines, collapsed


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
    any_dups = False
    for path in args.files:
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except FileNotFoundError:
            sys.stderr.write(f"# not present, skipping: {path}\n")
            continue
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
        new_lines, collapsed = dedup(lines, args.global_unique)
        if collapsed:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(new_lines))
            print(f"deduped {len(collapsed)} canonical name(s) in {path}:")
            for name, n, extra in collapsed:
                print(f"  {name}: {n} -> 1" + (f"  (+examples {extra})" if extra else ""))
        else:
            print(f"no duplicate canonicals ({scope}) in {path}")
    return 1 if (args.check and any_dups) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
