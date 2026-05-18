#!/usr/bin/env python3
"""Verify each source branch's brand-new ``##`` and ``### CANONICAL_NAME``
section headers survived into the post-merge file.

Background
----------

When many ``claude/<task-id>`` branches each contribute model-specific
covariate-section additions to ``inst/references/covariate-columns.md``,
the bulk merge resolves conflicts with ``-X theirs``. For *brand-new*
sections (i.e. ``### CANONICAL_NAME`` headers only one branch added
because it branched from older main), the later branches' versions
of the file — which lack those headers — silently overwrite the
addition.

The filename-only verifier in ``verify_branch_contributions.sh``
catches this case only when the ``.R`` model filename happens to be
unique to the new section. When the same ``.R`` file is also
referenced elsewhere (e.g. under WT, AGE, SEXF entries), the
filename check passes but the new ``### CANONICAL_NAME`` section is
lost. Real cases this caught on the 2026-05-17 consolidation:

* Tsuji 2017 linezolid → ``## Mixture / latent-class indicators`` +
  ``### MIX_PDI`` (caught because Tsuji_2017_linezolid.R was unique
  to that section — the filename check happened to catch it).
* van der Walt 2013 dapagliflozin → ``### HEPIMP_SEV`` +
  ``### HEPIMP_MODSEV`` (caught for the same accidental reason).
* Xia 2024 warfarin → ``### CYP2C9_S1_COUNT``, ``### CYP2C9_S2_COUNT``,
  ``### CYP2C9_S3_COUNT``, ``### VKORC1_1639G_COUNT`` (NOT caught:
  ``Xia_2024_warfarin.R`` is also referenced under AGE / SEXF / WT, so
  the filename check passed despite four lost sections).
* Delor 2013 Alzheimer's CDR-SOB → ``### T_ENTRY`` (also NOT caught,
  same reason).

This script closes that gap.

Algorithm
---------

For each candidate branch (pattern matches + ``--extra-ref`` entries):

1. Read the file at the branch tip (``git show <branch>:<file>``).
2. Read the file at the merge base (``git show <base>:<file>``).
3. Extract all ``##`` and ``###`` headers from each.
4. Compute ``new_headers = branch_headers - base_headers``.
5. Read the merged file at ``<repo>/.worktrees/<branch>/<file>``.
6. Extract all ``##`` and ``###`` headers from the merged file.
7. Assert ``new_headers - merged_headers == set()``.

The ``###`` regex captures only the canonical-name token (e.g.
``WT`` from ``### WT (**canonical for body weight ...**)``) so that
benign annotation drift between branch and merged file does NOT
register as a regression. Section identity is the token, not the
prose.

Exit codes
----------

* 0 — no missing headers (or no candidate branches, or merged file
  not present).
* 1 — at least one branch has new ``##`` / ``###`` headers absent
  from the merged file. Details printed to stdout.
* 2 — bad arguments.

Expected error format (sample)::

    ERROR: (section-verifier) 3 branch(es) have new section headers missing from inst/references/covariate-columns.md:
        claude/aa-bb-cc: ### HEPIMP_MODSEV, ### HEPIMP_SEV
        claude/dd-ee-ff: ### CYP2C9_S1_COUNT, ### CYP2C9_S2_COUNT, ### CYP2C9_S3_COUNT, ### VKORC1_1639G_COUNT
        claude/gg-hh-ii: ### T_ENTRY
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

H2_RE = re.compile(r"^## (.+)$", re.M)
H3_RE = re.compile(r"^### ([A-Za-z0-9_, ]+)\b", re.M)


def show_or_empty(ref_path: str, cwd: Path) -> str:
    r = subprocess.run(["git", "show", ref_path], cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        return ""
    return r.stdout


def extract_headers(text: str) -> tuple[set[str], set[str]]:
    return set(H2_RE.findall(text)), set(H3_RE.findall(text))


def candidate_branches(repo: Path, pattern: str, extra_refs: list[str]) -> list[str]:
    refspec = f"refs/remotes/{pattern}"
    r = subprocess.run(
        ["git", "for-each-ref", "--format=%(refname:short)", refspec],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    branches = sorted({b.strip() for b in r.stdout.splitlines() if b.strip()})
    for ref in extra_refs or []:
        if ref and ref not in branches:
            branches.append(ref)
    return branches


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Verify brand-new ##/### canonical-section headers survive a bulk -X theirs merge."
    )
    ap.add_argument("--repo", type=Path, required=True, help="Target git repo.")
    ap.add_argument(
        "--branch",
        required=True,
        help="Merge branch name (worktree at <repo>/.worktrees/<branch>).",
    )
    ap.add_argument("--base", default="origin/main", help="Merge base ref.")
    ap.add_argument(
        "--pattern",
        default="origin/claude/*",
        help="Source branch refspec under refs/remotes/.",
    )
    ap.add_argument("--file", required=True, help="Repo-relative file path.")
    ap.add_argument(
        "--extra-ref",
        action="append",
        default=[],
        help="Additional fully-qualified ref to include (repeatable).",
    )
    args = ap.parse_args(argv)

    repo: Path = args.repo
    worktree = repo / ".worktrees" / args.branch
    merged_path = worktree / args.file
    if not merged_path.exists():
        sys.stderr.write(
            f"# (section-verifier) merged file not present at {merged_path}; skipping.\n"
        )
        return 0

    base_text = show_or_empty(f"{args.base}:{args.file}", cwd=repo)
    base_h2, base_h3 = extract_headers(base_text)

    merged_text = merged_path.read_text()
    merged_h2, merged_h3 = extract_headers(merged_text)

    branches = candidate_branches(repo, args.pattern, args.extra_ref)

    failures: list[tuple[str, set[str], set[str]]] = []
    for br in branches:
        branch_text = show_or_empty(f"{br}:{args.file}", cwd=repo)
        if not branch_text:
            continue
        b_h2, b_h3 = extract_headers(branch_text)
        new_h2 = b_h2 - base_h2
        new_h3 = b_h3 - base_h3
        if not (new_h2 or new_h3):
            continue
        miss_h2 = new_h2 - merged_h2
        miss_h3 = new_h3 - merged_h3
        if miss_h2 or miss_h3:
            failures.append((br, miss_h2, miss_h3))

    if not failures:
        print(
            f"    (section-verifier) OK — all per-branch new ##/### canonical-section headers survived in {args.file}"
        )
        return 0

    print()
    print(
        f"ERROR: (section-verifier) {len(failures)} branch(es) have new section headers missing from {args.file}:"
    )
    for br, miss_h2, miss_h3 in failures:
        short = br[len("origin/") :] if br.startswith("origin/") else br
        parts = [f'## "{h}"' for h in sorted(miss_h2)]
        parts += [f"### {h}" for h in sorted(miss_h3)]
        print(f"    {short}: {', '.join(parts)}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
