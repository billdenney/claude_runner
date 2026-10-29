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
import posixpath
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


def normalise_path(path: str) -> str:
    """``path`` relative to the repository root, as a tree holds it: ``./``
    and doubled ``/`` dropped and ``..`` folded in. A GitError when nothing is
    left, it is absolute or climbs out of the root, it ends in ``/`` (a
    directory), it starts with ``:``, which git reads as pathspec magic, or
    it is a Windows path (a backslash, or a drive letter), which no tree
    holds."""
    if not path:
        raise GitError("an empty path names no file")
    if path.startswith(":"):
        raise GitError(f"{path!r} starts with ':', which git reads as pathspec magic")
    if path.endswith("/"):
        raise GitError(f"{path!r} names a directory, not a file")
    if "\\" in path:
        raise GitError(f"{path!r} holds a backslash; a path in git's trees uses '/'")
    if len(path) > 1 and path[1] == ":" and path[0].isascii() and path[0].isalpha():
        raise GitError(f"{path!r} starts with a drive letter; give it relative to the repository")
    norm = posixpath.normpath(path)
    if path.startswith("/") or norm == ".." or norm.startswith("../"):
        raise GitError(f"{path!r} is outside the repository")
    if norm == ".":
        raise GitError(f"{path!r} names no file")
    return norm


def read_file(repo: Path, commit: str, path: str) -> str | None:
    """``path`` as of ``commit``, or None when that commit has no such file; a
    GitError when the file, or a tree on the way to it, cannot be read."""
    path = normalise_path(path)
    if blob_id(repo, commit, path) is None:
        return None
    return git(repo, "cat-file", "blob", f"{commit}:{path}")


def blob_id(repo: Path, commit: str, path: str) -> str | None:
    """The id of ``path``'s blob at ``commit``, or None when it has no such file:
    the same id means the same text, so a reader can parse each once.

    ``git ls-tree`` lists nothing, and exits 0, for a path the commit's tree
    does not have (a path through a file included), and exits non-zero when
    the commit or a tree on the way cannot be read; ``rev-parse`` answers both
    the same way. A path naming a directory is a GitError too, and so is one
    normalise_path refuses. ``--full-tree`` reads the path from the root
    whatever directory ``repo`` is, and ``--literal-pathspecs`` reads it as
    written."""
    path = normalise_path(path)
    listed = git(repo, "--literal-pathspecs", "ls-tree", "--full-tree", "-z", commit, "--", path)
    for entry in listed.split("\0"):
        meta, _, name = entry.partition("\t")
        if name == path:
            _, kind, oid = meta.split()
            if kind != "blob":
                raise GitError(f"{path} at {commit} is a {kind}, not a file")
            return oid
    return None


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
                " did not merge (left out with --exclude-ref, or pushed after the survey): "
                + ", ".join(self.not_merged)
            )
        for member in self.members:
            if member.advanced:
                lines.append(
                    f"merge-set gate: {member.ref} moved on after {self.branch} merged it;"
                    f" reading the merged commit {member.merged[:12]}, not its tip"
                    f" {member.tip[:12]}"
                )
        return lines


def read_record(path: str) -> list[tuple[str, str]]:
    """The ``(ref, merged commit)`` pairs a consolidation run recorded, from the
    ``merged<TAB>ref<TAB>commit`` lines of its inputs file."""
    pairs = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        key, _, rest = line.partition("\t")
        if key == "merged":
            ref, _, sha = rest.partition("\t")
            pairs.append((ref, sha))
    if not pairs:
        raise GitError(f"{path} records no merged ref")
    return pairs


def _recorded(
    repo: Path, branch: str, base: str, refs: Sequence[str], record: Sequence[tuple[str, str]]
) -> MergeSet:
    """The merge set a run recorded: each ref at the commit it merged, whatever
    the ref holds now, so a ref rewritten or pruned since changes nothing."""
    base_sha = git(repo, "rev-parse", f"{base}^{{commit}}").strip()
    members = []
    for ref, sha in record:
        if _run(repo, ["cat-file", "-e", f"{sha}^{{commit}}"]).returncode != 0:
            raise GitError(f"the run merged {ref} at {sha}, which is not in {repo}'s objects")
        tip = sha
        if resolves(repo, ref):
            tip = git(repo, "rev-parse", f"{ref}^{{commit}}").strip()
        fork = merge_base(repo, base_sha, sha)
        if fork is None:
            raise GitError(f"{ref} was merged into {branch} but shares no history with {base}")
        members.append(Member(ref, tip, sha, fork))
    names = {ref for ref, _ in record}
    rest = [ref for ref in refs if ref not in names]
    tips = git(repo, "rev-parse", *(f"{ref}^{{commit}}" for ref in rest)).split() if rest else []
    on_base = [ref for ref, tip in zip(rest, tips, strict=True) if is_ancestor(repo, tip, base_sha)]
    not_merged = [ref for ref in rest if ref not in on_base]
    return MergeSet(branch, base, tuple(members), tuple(not_merged), tuple(on_base))


def compute(
    repo: Path,
    branch: str,
    base: str,
    refs: Sequence[str],
    record: Sequence[tuple[str, str]] | None = None,
) -> MergeSet:
    """Split ``refs`` into the members of ``branch``'s merge set and the rest.

    With ``record`` (read_record), the members are the refs it names at the
    commits it names, and ``refs`` only says which other candidates exist."""
    if record is not None:
        return _recorded(repo, branch, base, refs, record)
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


def add_record_argument(ap: argparse.ArgumentParser) -> None:
    """--merged-record, which every gate --finish runs takes (see read_record)."""
    ap.add_argument(
        "--merged-record",
        metavar="FILE",
        help="a consolidation run's inputs file: check the refs it merged, at the commits"
        " it merged, rather than what the refs hold now",
    )


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
    add_record_argument(ap)
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
    record = read_record(args.merged_record) if args.merged_record else None
    if not refs and record is None:
        return fail(f"no branch matches --pattern {args.pattern!r} and no --extra-ref was given")
    found = compute(repo, args.branch, args.base, refs, record)
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
