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

For each branch in the merge set (merge_set.py: the pattern matches and
``--extra-ref`` entries the consolidation actually merged; a branch left out
with ``--exclude-ref`` or pushed after the survey is not checked):

1. Read the file at the commit that was merged (the branch tip, or the part
   of it merged before the tip moved on).
2. Read the file at that commit's fork point from the base.
3. Extract all ``##`` and ``###`` headers from each.
4. Compute ``new_headers = branch_headers - fork_headers``: the headers the
   branch itself added. One it inherited and main has since renamed is not
   the branch's contribution, and is not expected in the merge.
5. Read the merged file at ``<repo>/.worktrees/<branch>/<file>``.
6. Extract all ``##`` and ``###`` headers from the merged file.
7. Assert ``new_headers - merged_headers == set()``.

The ``###`` regex captures only the canonical-name tokens (e.g.
``WT`` from ``### WT (**canonical for body weight ...**)``) so that
benign annotation drift between branch and merged file does NOT
register as a regression. Section identity is the token, not the
prose. A header naming several canonicals (``### QTc, QTcF, QTcI``)
yields one token per name, so a branch that adds a name to such a
header is checked for that name, and a merged header whose list grew
further still passes.

Exit codes
----------

* 0 — no missing headers, or the merged file is not in the worktree.
* 1 — at least one branch has new ``##`` / ``###`` headers absent
  from the merged file. Details printed to stdout.
* 2 — the check could not run: a bad argument, a ``--base``, ``--branch``
  or ``--extra-ref`` that does not resolve, no worktree for ``--branch``, no
  branch matching ``--pattern``, an empty merge set, or a crash. Checking no
  branch at all would report every header present, so that is an error, not
  a pass.

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
import traceback
from pathlib import Path

import merge_set

H2_RE = re.compile(r"^## (.+)$", re.M)
H3_RE = re.compile(r"^### ([A-Za-z0-9_, ]+)\b", re.M)


def fail(message: str) -> int:
    sys.stderr.write(f"ERROR: (section-verifier) {message}\n")
    return 2


def is_work_tree(repo: Path) -> bool:
    r = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"], cwd=repo, capture_output=True, text=True
    )
    return r.returncode == 0


def extract_headers(text: str) -> tuple[set[str], set[str]]:
    """The ``##`` headers, and the names in the ``###`` headers (one per name)."""
    names = {name.strip() for header in H3_RE.findall(text) for name in header.split(",")}
    return set(H2_RE.findall(text)), names - {""}


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
    if not repo.is_dir() or not is_work_tree(repo):
        return fail(f"--repo {repo} is not a git working tree")
    refs = [("--base", args.base), ("--branch", args.branch)]
    refs += [("--extra-ref", ref) for ref in args.extra_ref if ref]
    for flag, ref in refs:
        if not merge_set.resolves(repo, ref):
            return fail(f"{flag} {ref!r} does not resolve to a commit in {repo}")
    worktree = repo / ".worktrees" / args.branch
    if not worktree.is_dir():
        return fail(f"no worktree for --branch {args.branch!r} at {worktree}")
    branches = merge_set.candidate_refs(repo, args.pattern, args.extra_ref)
    if not branches:
        return fail(
            f"no branch matches --pattern {args.pattern!r} and no --extra-ref was given;"
            " nothing to verify"
        )
    merged_path = worktree / args.file
    if not merged_path.exists():
        sys.stderr.write(
            f"# (section-verifier) merged file not present at {merged_path}; skipping.\n"
        )
        return 0

    found = merge_set.compute(repo, args.branch, args.base, branches)
    for line in found.summary():
        print(f"    (section-verifier) {line}")
    if not found.members:
        return fail(
            merge_set.empty_message(args.pattern, args.branch, args.base) + "; nothing to verify"
        )

    merged_text = merged_path.read_text(encoding="utf-8")
    merged_h2, merged_h3 = extract_headers(merged_text)

    failures: list[tuple[str, set[str], set[str]]] = []
    for member in found.members:
        branch_text = merge_set.read_file(repo, member.merged, args.file)
        if not branch_text:
            continue
        b_h2, b_h3 = extract_headers(branch_text)
        f_h2, f_h3 = extract_headers(merge_set.read_file(repo, member.fork, args.file) or "")
        new_h2 = b_h2 - f_h2
        new_h3 = b_h3 - f_h3
        if not (new_h2 or new_h3):
            continue
        miss_h2 = new_h2 - merged_h2
        miss_h3 = new_h3 - merged_h3
        if miss_h2 or miss_h3:
            failures.append((member.ref, miss_h2, miss_h3))

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
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception:
        # Exit 1 means "headers missing"; a crash must not read as that verdict.
        traceback.print_exc()
        raise SystemExit(2) from None
