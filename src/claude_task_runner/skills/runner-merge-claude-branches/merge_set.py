#!/usr/bin/env python3
"""The merge set of a consolidation branch: which refs it merged, and what.

``merge_branches.sh`` folds each task branch in with a real merge, so whether a
ref was merged is a question about ancestry. The repair and verify helpers,
though, are handed ``--pattern`` (plus any ``--extra-ref``), and the pattern
also matches refs the consolidation did NOT merge:

* refs left out on purpose with ``merge_branches.sh --exclude-ref``;
* refs pushed after the survey, or pushed to again after they were merged;
* refs from earlier rounds, which ``--base`` already has.

Helpers that enumerated the pattern themselves read those refs as
contributions. On 2026-09-29 the 18 refs excluded from a 256-branch merge put
8 orphan ``**Example models:**`` entries into covariate-columns.md and 6
bullets into NEWS.md, and the verifiers reported dozens of "missing"
contributions from refs that were never merged. Every helper therefore asks
this module which refs count, and what each one contributed.

Definitions, for a candidate ref R, the consolidation branch B and the base:

merged commit
    M = ``git merge-base R B``. That is R's tip when R is an ancestor of B.
    When R's tip moved on after B merged it, M is the part B merged; the
    later commits were never merged and count for nothing.
merge set
    R is in it when M is not an ancestor of the base, i.e. B merged something
    of R's that the base does not have. A ref B never merged has M = its fork
    point, which the base has; a ref from an earlier round is on the base.
own diff
    From R's fork point F = ``git merge-base <base> M`` to M. Content already
    at F was inherited from main, not contributed by R.

Imported by the Python helpers. Run as a script, it prints one line per member,
``<ref> <merged commit> <fork point>``, for verify_branch_contributions.sh.

Exit codes (as a script): 0 done; 2 it could not run: a bad argument, a
``--base``, ``--branch`` or ``--extra-ref`` that does not resolve, no branch
matching ``--pattern``, an empty merge set, or a failed git command.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import traceback
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    """A git command failed. Callers exit 2: a failure is never an answer."""


def _run(repo: Path, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, encoding="utf-8"
    )


def git(repo: Path, *args: str) -> str:
    """The stdout of ``git <args>`` in ``repo``; GitError when it fails."""
    proc = _run(repo, args)
    if proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed (exit {proc.returncode}): {proc.stderr.strip()[:400]}"
        )
    return proc.stdout


def resolves(repo: Path, ref: str) -> bool:
    """True when ``ref`` names a commit in ``repo``."""
    proc = _run(repo, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
    return proc.returncode == 0 and bool(proc.stdout.strip())


def worktree_of(repo: Path, branch: str) -> Path | None:
    """The worktree that has ``branch`` checked out, or None.

    No fallback: guessing another worktree would read, and write, the wrong file.
    """
    candidate: Path | None = None
    for line in git(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            candidate = Path(line[len("worktree ") :])
        elif line == f"branch refs/heads/{branch}":
            return candidate
    return None


def is_ancestor(repo: Path, commit: str, of: str) -> bool:
    proc = _run(repo, ["merge-base", "--is-ancestor", commit, of])
    if proc.returncode not in (0, 1):
        raise GitError(
            f"git merge-base --is-ancestor {commit} {of} failed (exit {proc.returncode}):"
            f" {proc.stderr.strip()[:400]}"
        )
    return proc.returncode == 0


def merge_base(repo: Path, one: str, other: str) -> str | None:
    """The best common ancestor of two commits, or None when they share none."""
    proc = _run(repo, ["merge-base", one, other])
    if proc.returncode == 0:
        return proc.stdout.strip()
    if proc.returncode == 1 and not proc.stderr.strip():
        return None
    raise GitError(
        f"git merge-base {one} {other} failed (exit {proc.returncode}): {proc.stderr.strip()[:400]}"
    )


def read_file(repo: Path, commit: str, path: str) -> str | None:
    """``path`` as of ``commit``, or None when that commit has no such file."""
    probe = _run(repo, ["cat-file", "-e", f"{commit}:{path}"])
    if probe.returncode != 0:
        if not resolves(repo, commit):
            raise GitError(f"{commit!r} does not resolve to a commit in {repo}")
        return None
    return git(repo, "cat-file", "blob", f"{commit}:{path}")


def candidate_refs(repo: Path, pattern: str, extra_refs: Sequence[str]) -> list[str]:
    """Refs under ``refs/remotes/<pattern>``, sorted, then any extra refs not among them."""
    listing = git(repo, "for-each-ref", "--format=%(refname:short)", f"refs/remotes/{pattern}")
    refs = sorted({line.strip() for line in listing.splitlines() if line.strip()})
    for ref in extra_refs:
        if ref and ref not in refs:
            refs.append(ref)
    return refs


@dataclass(frozen=True)
class Member:
    """A ref the consolidation merged."""

    ref: str
    tip: str
    merged: str
    """The commit that was merged: the tip, or the part merged before the tip moved on."""
    fork: str
    """``git merge-base <base> <merged>``, where the ref's own diff starts."""

    @property
    def advanced(self) -> bool:
        return self.merged != self.tip


@dataclass(frozen=True)
class MergeSet:
    branch: str
    base: str
    members: tuple[Member, ...]
    not_merged: tuple[str, ...]
    """Candidates the branch never merged, or merged nothing of that the base lacks."""
    on_base: tuple[str, ...]
    """Candidates the base already has: earlier rounds. Not worth a line of output."""

    def summary(self) -> list[str]:
        """What the gate left out, and which members moved on; empty when neither."""
        lines = []
        if self.not_merged:
            lines.append(
                f"merge-set gate: skipped {len(self.not_merged)} branch(es) that {self.branch}"
                " did not merge (left out with --exclude-ref, or pushed after the survey)"
            )
        for member in self.members:
            if member.advanced:
                lines.append(
                    f"merge-set gate: {member.ref} moved on after {self.branch} merged it;"
                    f" reading the merged commit {member.merged[:12]}, not its tip"
                    f" {member.tip[:12]}"
                )
        return lines


def compute(repo: Path, branch: str, base: str, refs: Sequence[str]) -> MergeSet:
    """Split ``refs`` into the members of ``branch``'s merge set and the rest."""
    if not refs:
        return MergeSet(branch, base, (), (), ())
    shas = git(repo, "rev-parse", *(f"{ref}^{{commit}}" for ref in (branch, base, *refs)))
    branch_sha, base_sha, *tips = shas.split()
    members: list[Member] = []
    not_merged: list[str] = []
    on_base: list[str] = []
    for ref, tip in zip(refs, tips, strict=True):
        if is_ancestor(repo, tip, base_sha):
            on_base.append(ref)
            continue
        merged = tip if is_ancestor(repo, tip, branch_sha) else merge_base(repo, tip, branch_sha)
        if merged is None or is_ancestor(repo, merged, base_sha):
            not_merged.append(ref)
            continue
        fork = merge_base(repo, base_sha, merged)
        if fork is None:
            raise GitError(f"{ref} was merged into {branch} but shares no history with {base}")
        members.append(Member(ref, tip, merged, fork))
    return MergeSet(branch, base, tuple(members), tuple(not_merged), tuple(on_base))


def empty_message(pattern: str, branch: str, base: str) -> str:
    """The error for a merge set with no members: there is nothing to check."""
    return (
        f"no branch matching --pattern {pattern!r} or given with --extra-ref is in the merge"
        f" set of {branch}: it merged none of them, or {base} already has them"
    )


def touches(repo: Path, member: Member, path: str) -> bool:
    """True when the member's own diff changes ``path``."""
    proc = _run(repo, ["diff", "--quiet", member.fork, member.merged, "--", path])
    if proc.returncode not in (0, 1):
        raise GitError(
            f"git diff --quiet {member.fork} {member.merged} -- {path} failed"
            f" (exit {proc.returncode}): {proc.stderr.strip()[:400]}"
        )
    return proc.returncode == 1


def fail(message: str) -> int:
    print(f"ERROR: (merge-set) {message}", file=sys.stderr)
    return 2


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Print the merge set of a consolidation branch: one line per member,"
        " '<ref> <merged commit> <fork point>'."
    )
    ap.add_argument("--repo", required=True)
    ap.add_argument("--branch", required=True, help="the consolidation branch")
    ap.add_argument("--base", default="origin/main")
    ap.add_argument("--pattern", default="origin/claude/*")
    ap.add_argument(
        "--extra-ref",
        action="append",
        default=[],
        help="additional ref to consider, e.g. a hand-picked branch outside --pattern (repeatable)",
    )
    ap.add_argument(
        "--quiet", action="store_true", help="do not print what the gate left out, on stderr"
    )
    args = ap.parse_args(argv)

    repo = Path(args.repo)
    if not repo.is_dir():
        return fail(f"--repo {repo} is not a directory")
    named = [("--base", args.base), ("--branch", args.branch)]
    named += [("--extra-ref", ref) for ref in args.extra_ref if ref]
    for flag, ref in named:
        if not resolves(repo, ref):
            return fail(f"{flag} {ref!r} does not resolve to a commit in {repo}")
    refs = candidate_refs(repo, args.pattern, args.extra_ref)
    if not refs:
        return fail(f"no branch matches --pattern {args.pattern!r} and no --extra-ref was given")
    found = compute(repo, args.branch, args.base, refs)
    if not args.quiet:
        for line in found.summary():
            print(f"# {line}", file=sys.stderr)
    if not found.members:
        return fail(empty_message(args.pattern, args.branch, args.base))
    for member in found.members:
        print(f"{member.ref} {member.merged} {member.fork}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:
        traceback.print_exc()
        sys.exit(2)
