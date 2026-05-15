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

1. Reads the file at ``origin/<base>`` as the merge base.
2. For each ``<pattern>`` branch that touched the file, reads its
   tip version.
3. Parses every ``**Example models:**`` line and buckets the
   ``(filename, annotation)`` entries by ``(covariate header,
   subsection header)`` resolved from the most recent ``##``/``###``
   markdown headings.
4. Builds a union across base + all branches, taking the longest
   (most informative) annotation per filename per bucket.
5. Walks the CURRENT (post-merge) file and re-emits each
   Example-models line as a single deduplicated list, preserving
   the original line prefix.
6. Writes the result back to the merge branch's worktree.

Out-of-scope additions (brand-new lines, new sections, table rows
that aren't Example-models lines) are NOT touched — the merge
already handles those via standard 3-way merging because they
appear at unique line positions per branch.

If the file's structured-markdown shape differs from
"Example models" lines, extend ``EXAMPLE_LINE_RE`` to a list of
patterns and add per-pattern parser functions. Open to PRs.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# Regex for the structured line we union-merge. Currently only one
# shape is supported; the regex is permissive on whitespace and bullet
# style (allows ``- ``, ``* `` or numbered) so it picks up most
# reasonable markdown.
EXAMPLE_LINE_RE = re.compile(r"^\s*(?:[-*]|\d+\.)\s*\*\*Example models:\*\*\s*(.*)$")
SECTION_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def run(args: list[str], cwd: Path) -> str:
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(
            f"{' '.join(args)} failed (exit {r.returncode}): {r.stderr[:400]}"
        )
    return r.stdout


def list_pattern_branches(repo: Path, pattern: str) -> list[str]:
    """Return remote branches matching the configured glob (e.g.
    ``origin/claude/*``)."""
    refspec = f"refs/remotes/{pattern}"
    out = run(
        ["git", "for-each-ref", "--format=%(refname:short)", refspec],
        cwd=repo,
    )
    return sorted(b.strip() for b in out.splitlines() if b.strip())


def branches_touching(repo: Path, base: str, pattern: str, file_rel: str) -> list[str]:
    """Filter to branches whose tip has any commit modifying ``file_rel``
    vs the configured base."""
    out = []
    for br in list_pattern_branches(repo, pattern):
        d = run(["git", "diff", "--name-only", f"{base}..{br}", "--", file_rel], cwd=repo)
        if d.strip():
            out.append(br)
    return out


def parse_example_models(body: str) -> list[tuple[str, str]]:
    """Parse a body string like::

        `A.R` (annot), `B.R`, `C.R` (annot)

    into ``[("A.R", "(annot)"), ("B.R", ""), ("C.R", "(annot)")]``.

    Handles balanced single-level nested parens inside annotations.
    Returns the entries in the order they appeared.
    """
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(body):
        m = re.search(r"`([^`]+\.R)`", body[i:])
        if not m:
            break
        fname = m.group(1)
        cursor = i + m.end()
        # Skip whitespace.
        while cursor < len(body) and body[cursor] in " \t":
            cursor += 1
        annotation = ""
        if cursor < len(body) and body[cursor] == "(":
            depth = 0
            start = cursor
            while cursor < len(body):
                ch = body[cursor]
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        cursor += 1
                        break
                cursor += 1
            annotation = body[start:cursor]
        out.append((fname, annotation))
        # Skip comma + whitespace separator.
        while cursor < len(body) and body[cursor] in ", \t":
            cursor += 1
        i = cursor
    return out


def section_of_line(file_text: str, line_idx: int) -> tuple[str, str]:
    """Return ``(cov_section, subsection)`` for the line at ``line_idx``.

    Tracks the most recent ``##``-level header (covariate) and the most
    recent deeper header (subsection). The bucket key is the pair so
    the same subsection name under different covariates is kept
    distinct.
    """
    cov = ""
    sub = ""
    for i, line in enumerate(file_text.splitlines()):
        if i > line_idx:
            break
        m = SECTION_RE.match(line)
        if not m:
            continue
        depth = len(m.group(1))
        name = m.group(2)
        if depth == 2:
            cov = name
            sub = ""
        elif depth >= 3:
            sub = name
    return cov, sub


def collect_entries(
    *,
    repo: Path,
    base: str,
    branches: list[str],
    file_rel: str,
) -> dict[tuple[str, str], dict[str, str]]:
    """Walk base + all branches and collect ``(cov, sub) -> {fname: annotation}``.

    Across versions, the LONGEST observed annotation per ``(cov, sub,
    fname)`` wins. The reasoning: each branch's annotation is its
    model-specific note; longer non-empty annotations are strictly
    more informative.
    """
    entries: dict[tuple[str, str], dict[str, str]] = {}

    versions: list[tuple[str, str]] = []
    versions.append(("BASE", run(["git", "show", f"{base}:{file_rel}"], cwd=repo)))
    for br in branches:
        try:
            versions.append((br, run(["git", "show", f"{br}:{file_rel}"], cwd=repo)))
        except RuntimeError:
            # Branch may have deleted the file; rare. Skip.
            continue

    for _label, text in versions:
        for i, line in enumerate(text.splitlines(keepends=False)):
            m = EXAMPLE_LINE_RE.match(line)
            if not m:
                continue
            cov, sub = section_of_line(text, i)
            bucket = entries.setdefault((cov, sub), {})
            for fname, annot in parse_example_models(m.group(1)):
                cur = bucket.get(fname)
                if cur is None or len(annot) > len(cur):
                    bucket[fname] = annot

    return entries


def emit_merged(
    *,
    current_text: str,
    entries: dict[tuple[str, str], dict[str, str]],
    branch_files: dict[str, str],
    base_text: str,
) -> str:
    """Rewrite each Example-models line in ``current_text`` to the union.

    Ordering: each line is rebuilt as ``ordered_filenames`` derived
    from (a) the current line's order, then (b) any new filenames
    appearing in branch versions in branch order, then (c) any new
    ones from base. This minimises diff churn against the
    post-``-X-theirs`` file while still folding in every branch's
    additions.
    """
    current_lines = current_text.splitlines(keepends=False)
    out_lines: list[str] = []

    for i, line in enumerate(current_lines):
        m = EXAMPLE_LINE_RE.match(line)
        if not m:
            out_lines.append(line)
            continue
        cov, sub = section_of_line(current_text, i)
        bucket = entries.get((cov, sub))
        if not bucket:
            out_lines.append(line)
            continue

        ordered: list[str] = []
        # 1: current order.
        for fname, _ in parse_example_models(m.group(1)):
            if fname not in ordered:
                ordered.append(fname)
        # 2: branch order.
        for _br, text in branch_files.items():
            for j, bl in enumerate(text.splitlines(keepends=False)):
                bm = EXAMPLE_LINE_RE.match(bl)
                if not bm:
                    continue
                bcov, bsub = section_of_line(text, j)
                if (bcov, bsub) != (cov, sub):
                    continue
                for fname, _ in parse_example_models(bm.group(1)):
                    if fname not in ordered:
                        ordered.append(fname)
        # 3: base order.
        for j, bl in enumerate(base_text.splitlines(keepends=False)):
            bm = EXAMPLE_LINE_RE.match(bl)
            if not bm:
                continue
            bcov, bsub = section_of_line(base_text, j)
            if (bcov, bsub) != (cov, sub):
                continue
            for fname, _ in parse_example_models(bm.group(1)):
                if fname not in ordered:
                    ordered.append(fname)

        # Compose.
        prefix_end = line.index("**Example models:**") + len("**Example models:**")
        prefix = line[:prefix_end]
        merged_body_parts = []
        for fname in ordered:
            annot = bucket.get(fname, "")
            piece = f"`{fname}`"
            if annot:
                piece += f" {annot}"
            merged_body_parts.append(piece)
        merged_body = ", ".join(merged_body_parts) + "."
        out_lines.append(f"{prefix} {merged_body}")

    return "\n".join(out_lines) + ("\n" if current_text.endswith("\n") else "")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", type=Path, required=True,
                    help="Repo path (contains .git and the worktree).")
    ap.add_argument("--branch", required=True,
                    help="The merge branch name (worktree at <repo>/.worktrees/<branch>).")
    ap.add_argument("--base", default="origin/main",
                    help="Base ref (default: origin/main).")
    ap.add_argument("--pattern", default="origin/claude/*",
                    help="Source branch refspec under refs/remotes/ (default: origin/claude/*).")
    ap.add_argument("--file", required=True,
                    help="Repo-relative path to the union-merge target file.")
    args = ap.parse_args(argv)

    repo: Path = args.repo
    worktree = repo / ".worktrees" / args.branch
    target = worktree / args.file
    if not target.exists():
        sys.stderr.write(f"# target file not present on branch: {target}\n")
        return 0  # nothing to do

    branches = branches_touching(repo, args.base, args.pattern, args.file)
    sys.stderr.write(f"# branches touching {args.file}: {len(branches)}\n")
    for b in branches:
        sys.stderr.write(f"#   - {b}\n")
    if not branches:
        sys.stderr.write("# no branches touched the union-file; nothing to reconstruct.\n")
        return 0

    base_text = run(["git", "show", f"{args.base}:{args.file}"], cwd=repo)
    branch_files = {br: run(["git", "show", f"{br}:{args.file}"], cwd=repo)
                    for br in branches}

    entries = collect_entries(
        repo=repo,
        base=args.base,
        branches=branches,
        file_rel=args.file,
    )
    sys.stderr.write(f"# (cov, sub) buckets with entries: {len(entries)}\n")
    sys.stderr.write(f"# total filename entries:           "
                     f"{sum(len(v) for v in entries.values())}\n")

    current_text = target.read_text()
    new_text = emit_merged(
        current_text=current_text,
        entries=entries,
        branch_files=branch_files,
        base_text=base_text,
    )
    target.write_text(new_text)
    sys.stderr.write(f"# wrote merged file: {target}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
