"""Reclaim the git worktrees of finished tasks (ADR-0034).

A queue whose pre-dispatch hook creates one git worktree per task (ADR-0013)
accumulates them without bound. On 2026-09-25 the nlmixr2lib ingestion queue
had 305 worktrees holding 36 GB; 244 belonged to tasks that were ``completed``
and whose branch a consolidation merge had folded into ``origin/main`` weeks
earlier. The runner never removed one on its own initiative, because a
worktree can hold the only copy of unpushed work (ADR-0020, ADR-0032). This
module removes a worktree only when it can PROVE nothing is lost:

1. the task's state says ``completed``. ``awaiting_sidecar``, ``running``,
   ``failed``, ``deferred`` and every other status keep their worktree:
   ``claude --resume`` and a retry need the directory. No dispatch thread may
   still hold the task either, because the dispatcher writes ``completed``
   BEFORE it runs the post-dispatch hook inside the worktree;
2. the worktree has the task's branch checked out, and that branch is an
   ancestor of ``<remote>/<parent_branch>`` right after a fetch, so every
   commit on it already lives on the remote's parent branch;
3. ``git status --porcelain`` is empty, apart from untracked paths the
   operator declared disposable (``discardable_untracked``). Those are
   removed with ``git worktree remove --force``; nothing else ever is.
   Ignored files do not count as work (git's own ``worktree remove`` deletes
   them too), except a declared ``deliverable_paths`` entry inside the
   worktree: the task's own output is never discarded as build debris.

The task status and the working tree are checked again immediately before
each removal, under the hook's ``flock`` when ``lock_file`` is set. The local
branch is deleted with ``git branch -d``, never ``-D``, so git refuses on its
own when the branch is not merged into its upstream.

Nothing else is touched: no ``git worktree prune``, no remote branches, no
worktree that no task YAML in ``todo/`` names, and never a repository's main
worktree or a worktree someone locked with ``git worktree lock``.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import subprocess
import time
from collections import Counter, defaultdict
from collections.abc import Collection, Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from claude_task_runner.config.schema import WorktreeReclaimSettings
from claude_task_runner.queue.store import (
    QueueIOError,
    QueueSchemaError,
    list_pending_tasks,
    load_state,
    load_task,
    state_path_for,
)

logger = logging.getLogger(__name__)

RECLAIMABLE_STATUS = "completed"
"""The only task status whose worktree may be reclaimed."""

_LOCK_POLL_S = 0.1
"""Retry cadence of the non-blocking ``flock`` loop. The wait itself is
bounded by ``[worktree_reclaim].lock_timeout_s``."""

_DIRT_SHOWN = 3
"""How many blocking ``git status`` entries a DIRTY keep reason quotes."""


class ReclaimError(RuntimeError):
    """The reclaim cannot run at all, e.g. the directory is not a queue."""


class Outcome(StrEnum):
    """What happened to one task's worktree."""

    RECLAIMED = "reclaimed"
    """Removed. ``branch_deleted`` says whether ``git branch -d`` also went."""

    WOULD_RECLAIM = "would_reclaim"
    """Dry run: every condition holds, nothing was touched."""

    KEPT = "kept"
    """A condition failed (see ``reason``); the worktree is untouched."""

    FAILED = "failed"
    """Every condition held but ``git worktree remove`` itself failed."""


class KeepReason(StrEnum):
    """Why a worktree was kept."""

    STATUS = "status"
    """The task is not ``completed``, has no state file, or it is unreadable."""

    IN_FLIGHT = "in_flight"
    """A dispatch thread still holds the task (its post-dispatch hook runs
    inside the worktree after the state already says ``completed``)."""

    SHARED_WORKING_DIR = "shared_working_dir"
    """Another task YAML names the same working_dir."""

    NOT_LINKED_WORKTREE = "not_linked_worktree"
    """The working_dir is a repository's main worktree, or not a worktree git
    has registered. Only linked worktrees are ever removed."""

    LOCKED = "locked"
    """Someone ran ``git worktree lock`` on it."""

    BRANCH_MISMATCH = "branch_mismatch"
    """The worktree has another branch, or a detached HEAD, checked out."""

    UNMERGED = "unmerged"
    """The branch is not an ancestor of ``<remote>/<parent_branch>``."""

    DIRTY = "dirty"
    """``git status --porcelain`` shows work outside ``discardable_untracked``,
    or a declared deliverable inside the worktree is gitignored."""

    LOCK_BUSY = "lock_busy"
    """``lock_file`` stayed held past ``lock_timeout_s``; retried next pass."""

    LIMIT = "limit"
    """The pass reached its removal limit; retried next pass."""

    GONE = "gone"
    """The worktree vanished between the checks and the removal."""

    FETCH_FAILED = "fetch_failed"
    """``git fetch`` failed, so the merge check cannot be trusted."""

    GIT_ERROR = "git_error"
    """A read-only git probe failed on this worktree."""


ERROR_REASONS = frozenset({KeepReason.FETCH_FAILED, KeepReason.GIT_ERROR})
"""Keep reasons that mean something is broken, not merely "not yet"."""


@dataclass(frozen=True)
class ReclaimResult:
    """The verdict on one task's worktree."""

    task_id: str
    working_dir: str
    outcome: Outcome
    branch: str | None = None
    reason: KeepReason | None = None
    detail: str = ""
    forced: bool = False
    """``git worktree remove --force`` was (or would be) needed, only ever
    because of ``discardable_untracked`` paths."""
    discarded: tuple[str, ...] = ()
    """The untracked paths the removal discards (or would discard)."""
    branch_deleted: bool | None = None
    """Whether ``git branch -d`` deleted the local branch. ``None`` when no
    removal happened (kept, failed, or a dry run)."""

    @property
    def is_error(self) -> bool:
        return self.outcome is Outcome.FAILED or self.reason in ERROR_REASONS

    def to_json(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "working_dir": self.working_dir,
            "branch": self.branch,
            "outcome": self.outcome.value,
            "reason": self.reason.value if self.reason is not None else None,
            "detail": self.detail,
            "forced": self.forced,
            "discarded": list(self.discarded),
            "branch_deleted": self.branch_deleted,
        }


@dataclass(frozen=True)
class ReclaimReport:
    """Everything one reclaim pass saw and did."""

    applied: bool
    remote: str
    parent_branch: str
    tasks_scanned: int
    """Task YAMLs read from ``todo/``, parseable or not."""
    results: tuple[ReclaimResult, ...]
    """One entry per task whose working_dir has a checkout on disk."""
    unparseable_tasks: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    """Repository-level failures, e.g. a fetch that did not complete."""

    @property
    def ok(self) -> bool:
        """False when anything went wrong, as opposed to merely being kept."""
        return not self.errors and not any(r.is_error for r in self.results)

    def counts(self) -> dict[str, object]:
        outcomes = Counter(r.outcome for r in self.results)
        reasons = Counter(r.reason.value for r in self.results if r.reason is not None)
        return {
            "seen": len(self.results),
            "reclaimed": outcomes[Outcome.RECLAIMED],
            "would_reclaim": outcomes[Outcome.WOULD_RECLAIM],
            "kept": outcomes[Outcome.KEPT],
            "failed": outcomes[Outcome.FAILED],
            "branch_kept": sum(1 for r in self.results if r.branch_deleted is False),
            "kept_by_reason": dict(sorted(reasons.items())),
        }

    def summary(self) -> str:
        """One line in the shape of the queue's original shell script."""
        outcomes = Counter(r.outcome for r in self.results)
        reasons = Counter(r.reason for r in self.results if r.reason is not None)
        headline = (KeepReason.STATUS, KeepReason.UNMERGED, KeepReason.DIRTY)
        other = sum(n for reason, n in reasons.items() if reason not in headline)
        if self.applied:
            mode, done = "applied", f"{outcomes[Outcome.RECLAIMED]} reclaimed"
        else:
            mode, done = "dry run", f"{outcomes[Outcome.WOULD_RECLAIM]} reclaimable"
        line = (
            f"{mode}: {len(self.results)} worktree(s) seen; {done}; "
            f"kept {reasons[KeepReason.STATUS]} (status), "
            f"{reasons[KeepReason.UNMERGED]} (unmerged), "
            f"{reasons[KeepReason.DIRTY]} (uncommitted work), {other} (other); "
            f"{outcomes[Outcome.FAILED]} failed"
        )
        branch_kept = sum(1 for r in self.results if r.branch_deleted is False)
        if branch_kept:
            line += f"; {branch_kept} branch(es) kept by git branch -d"
        return line

    def to_json(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "applied": self.applied,
            "remote": self.remote,
            "parent_branch": self.parent_branch,
            "tasks_scanned": self.tasks_scanned,
            "unparseable_tasks": list(self.unparseable_tasks),
            "errors": list(self.errors),
            "counts": self.counts(),
            "results": [r.to_json() for r in self.results],
        }


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _GitResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def message(self) -> str:
        return (self.stderr or self.stdout).strip() or f"exit status {self.returncode}"


def _git(args: list[str], *, cwd: Path, timeout_s: float) -> _GitResult:
    """Run ``git <args>`` in ``cwd``. Never raises: failures come back as data."""
    env = dict(os.environ)
    # A supervisor has no terminal: a fetch that wanted credentials would
    # otherwise block on a prompt until the timeout.
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            encoding="utf-8",
            errors="surrogateescape",
            check=False,
            timeout=timeout_s,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return _GitResult(-1, "", f"git {' '.join(args)} timed out after {timeout_s:g}s")
    except OSError as exc:
        return _GitResult(-1, "", f"cannot run git: {exc}")
    return _GitResult(proc.returncode, proc.stdout, proc.stderr)


@dataclass(frozen=True)
class _WorktreeRecord:
    """One entry of ``git worktree list --porcelain``."""

    path: Path
    branch: str | None
    """Short branch name; ``None`` for a detached HEAD."""
    is_main: bool
    bare: bool
    locked: str | None
    """Lock reason (``""`` when locked without one); ``None`` when unlocked."""


def _parse_worktree_list(porcelain: str) -> list[_WorktreeRecord]:
    records: list[_WorktreeRecord] = []
    for block in porcelain.split("\n\n"):
        path: str | None = None
        branch: str | None = None
        locked: str | None = None
        bare = False
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            if key == "worktree":
                path = value
            elif key == "branch":
                branch = value.removeprefix("refs/heads/")
            elif key == "bare":
                bare = True
            elif key == "locked":
                locked = value
        if path is None:
            continue
        records.append(
            _WorktreeRecord(
                path=Path(os.path.realpath(path)),
                branch=branch,
                is_main=not records,
                bare=bare,
                locked=locked,
            )
        )
    return records


class _LockBusy(Exception):
    """``lock_file`` stayed held for longer than ``lock_timeout_s``."""


@contextlib.contextmanager
def _hook_lock(path: Path | None, timeout_s: float) -> Iterator[None]:
    """Hold the pre-dispatch hook's ``flock`` (a no-op when unconfigured).

    ``fcntl.flock`` and the ``flock(1)`` utility the hook uses are the same
    ``flock(2)`` lock, so the two exclude each other. Waits at most
    ``timeout_s``, then raises :class:`_LockBusy`.
    """
    if path is None:
        yield
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        opened = path.open("a")
    except OSError as exc:
        # A misconfigured lock_file is a could-not-run error, not a
        # per-worktree verdict; it fails before the first removal because
        # the fetch takes the lock first.
        raise ReclaimError(f"cannot open lock_file {path}: {exc}") from exc
    with opened as handle:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise _LockBusy(f"{path} still held after {timeout_s:g}s") from None
                time.sleep(_LOCK_POLL_S)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class _ProbeError(Exception):
    """A read-only git probe failed; the worktree is kept and reported."""


def _is_discardable(path: str, allowed: Collection[str]) -> bool:
    for entry in allowed:
        prefix = entry if entry.endswith("/") else entry + "/"
        if path == entry or path.startswith(prefix):
            return True
    return False


def _worktree_dirt(
    worktree: Path, settings: WorktreeReclaimSettings
) -> tuple[list[str], list[str]]:
    """Split ``git status --porcelain`` into (blocking, discardable) entries.

    The flags mirror git's own clean check in ``git worktree remove``, except
    that ``--untracked-files=normal`` is forced so a repository configured
    with ``status.showUntrackedFiles=no`` cannot hide untracked work.
    Ignored files are not listed, and ``git worktree remove`` deletes them
    too: git treats them as disposable, and so does this module.
    """
    res = _git(
        [
            "--no-optional-locks",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=normal",
            "--ignore-submodules=none",
        ],
        cwd=worktree,
        timeout_s=settings.git_timeout_s,
    )
    if res.returncode != 0:
        raise _ProbeError(f"git status: {res.message}")
    blocking: list[str] = []
    discardable: list[str] = []
    fields = res.stdout.split("\0")
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if not entry:
            continue
        if len(entry) < 4:
            blocking.append(entry)  # unparseable: never read as clean
            continue
        xy, path = entry[:2], entry[3:]
        if "R" in xy or "C" in xy:
            i += 1  # with -z, a rename's or copy's source path is the next field
        if xy == "??" and _is_discardable(path, settings.discardable_untracked):
            discardable.append(path)
        else:
            blocking.append(f"{xy} {path}")
    return blocking, discardable


def _describe_dirt(blocking: list[str]) -> str:
    shown = "; ".join(blocking[:_DIRT_SHOWN])
    more = len(blocking) - _DIRT_SHOWN
    return f"uncommitted: {shown}" + (f" (+{more} more)" if more > 0 else "")


# ---------------------------------------------------------------------------
# queue side
# ---------------------------------------------------------------------------


def require_queue_dir(queue_dir: Path) -> Path:
    """Resolve ``queue_dir`` and fail loudly unless it looks like a queue.

    Without this, a mistyped ``--queue`` would scan an empty ``todo/`` and
    report "0 worktrees seen" -- indistinguishable from a clean queue.
    """
    resolved = queue_dir.resolve()
    if not (resolved / "todo").is_dir():
        raise ReclaimError(f"{resolved} is not a queue directory: it has no todo/ subdirectory")
    return resolved


def _task_status(queue_dir: Path, task_id: str) -> tuple[str | None, str]:
    """``(status, "")``, or ``(None, why)`` when there is no usable state."""
    path = state_path_for(queue_dir, task_id)
    if not path.exists():
        return None, "no state file"
    try:
        return load_state(path).status, ""
    except (QueueIOError, QueueSchemaError) as exc:
        return None, f"state unreadable: {exc}"


@dataclass(frozen=True)
class _TaskEntry:
    task_id: str
    working_dir: Path
    deliverables: tuple[str, ...]
    """Declared ``deliverable_paths`` that fall inside the working_dir,
    relative to it. Outside ones (e.g. a report in the queue dir) cannot be
    affected by removing the worktree."""


def _inside(path: Path, root: Path) -> str | None:
    """``path`` relative to ``root`` (symlinks resolved), or None if outside."""
    try:
        rel = Path(os.path.realpath(path)).relative_to(os.path.realpath(root))
    except ValueError:
        return None
    return rel.as_posix()


def _task_entries(queue_dir: Path) -> tuple[list[_TaskEntry], list[str], int]:
    """Every parseable ``todo/`` task that names a working_dir."""
    entries: list[_TaskEntry] = []
    unparseable: list[str] = []
    scanned = 0
    for path in list_pending_tasks(queue_dir):
        scanned += 1
        try:
            task = load_task(path)
        except (QueueIOError, QueueSchemaError) as exc:
            logger.warning("worktree reclaim: skipping unparseable task %s: %s", path, exc)
            unparseable.append(path.stem)
            continue
        if task.working_dir is None:
            continue
        working_dir = task.working_dir.expanduser()
        if not working_dir.is_absolute():
            logger.warning(
                "worktree reclaim: task %s has a relative working_dir %s; skipped",
                task.id,
                working_dir,
            )
            continue
        inside = (
            _inside(d.expanduser() if d.is_absolute() else working_dir / d, working_dir)
            for d in task.deliverable_paths
        )
        entries.append(
            _TaskEntry(
                task_id=task.id,
                working_dir=working_dir,
                deliverables=tuple(rel for rel in inside if rel is not None),
            )
        )
    return entries, unparseable, scanned


def _lock_path(queue_dir: Path, settings: WorktreeReclaimSettings) -> Path | None:
    raw = settings.lock_file.strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path if path.is_absolute() else queue_dir / path


# ---------------------------------------------------------------------------
# the pass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Candidate:
    """A worktree that passed every check that needs no fetch."""

    task_id: str
    working_dir: Path
    branch: str
    worktree_path: Path
    """The path as git registered it (symlinks resolved)."""
    repo_root: Path
    """The repository's main worktree (or bare dir): where repo-level
    commands -- fetch, ``worktree remove``, ``branch -d`` -- run."""
    deliverables: tuple[str, ...] = ()


def _kept(
    task_id: str,
    working_dir: Path,
    reason: KeepReason,
    detail: str,
    *,
    branch: str | None = None,
) -> ReclaimResult:
    return ReclaimResult(
        task_id=task_id,
        working_dir=str(working_dir),
        outcome=Outcome.KEPT,
        branch=branch,
        reason=reason,
        detail=detail,
    )


def _screen(
    entry: _TaskEntry,
    *,
    queue_dir: Path,
    settings: WorktreeReclaimSettings,
    in_flight: frozenset[str],
    owners: dict[str, list[str]],
    registries: dict[Path, list[_WorktreeRecord] | str],
) -> ReclaimResult | _Candidate:
    """Apply every check that needs no fetch, cheapest first."""
    task_id, working_dir = entry.task_id, entry.working_dir
    status, why = _task_status(queue_dir, task_id)
    if status != RECLAIMABLE_STATUS:
        return _kept(task_id, working_dir, KeepReason.STATUS, f"status={status}" if status else why)
    if task_id in in_flight:
        return _kept(
            task_id, working_dir, KeepReason.IN_FLIGHT, "a dispatch thread still holds the task"
        )
    sharers = owners[os.path.realpath(working_dir)]
    if len(sharers) > 1:
        return _kept(
            task_id,
            working_dir,
            KeepReason.SHARED_WORKING_DIR,
            f"working_dir is named by {len(sharers)} tasks: {', '.join(sorted(sharers))}",
        )
    if not (working_dir / ".git").is_file():
        return _kept(
            task_id,
            working_dir,
            KeepReason.NOT_LINKED_WORKTREE,
            "a repository's main worktree (.git is a directory), not a linked worktree",
        )

    probe = _git(
        ["rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=working_dir,
        timeout_s=settings.git_timeout_s,
    )
    if probe.returncode != 0:
        return _kept(task_id, working_dir, KeepReason.GIT_ERROR, f"git rev-parse: {probe.message}")
    common_dir = Path(os.path.realpath(probe.stdout.strip()))
    registry = registries.get(common_dir)
    if registry is None:
        listing = _git(
            ["worktree", "list", "--porcelain"], cwd=working_dir, timeout_s=settings.git_timeout_s
        )
        registry = (
            _parse_worktree_list(listing.stdout)
            if listing.returncode == 0
            else f"git worktree list: {listing.message}"
        )
        registries[common_dir] = registry
    if isinstance(registry, str):
        return _kept(task_id, working_dir, KeepReason.GIT_ERROR, registry)

    real = Path(os.path.realpath(working_dir))
    record = next((r for r in registry if r.path == real), None)
    if record is None or record.is_main or record.bare:
        return _kept(
            task_id,
            working_dir,
            KeepReason.NOT_LINKED_WORKTREE,
            f"not a linked worktree of {common_dir}",
        )
    if record.locked is not None:
        return _kept(
            task_id,
            working_dir,
            KeepReason.LOCKED,
            f"git worktree lock: {record.locked or 'no reason given'}",
            branch=record.branch,
        )
    expected = settings.render_branch(task_id=task_id, worktree_name=working_dir.name)
    if record.branch != expected:
        actual = "a detached HEAD" if record.branch is None else f"branch {record.branch}"
        return _kept(
            task_id,
            working_dir,
            KeepReason.BRANCH_MISMATCH,
            f"{actual} is checked out; expected {expected}",
            branch=record.branch,
        )
    return _Candidate(
        task_id=task_id,
        working_dir=working_dir,
        branch=expected,
        worktree_path=record.path,
        repo_root=registry[0].path,
        deliverables=entry.deliverables,
    )


def _fetch(repo_root: Path, settings: WorktreeReclaimSettings, lock: Path | None) -> str | None:
    """Refresh ``<remote>/<parent_branch>``; return an error message or None.

    The explicit refspec updates the remote-tracking ref however the remote's
    own fetch refspec is configured. Runs under the hook lock because the hook
    fetches the same ref, and two concurrent fetches can fail on its ref lock.
    """
    parent = settings.parent_branch
    refspec = f"+refs/heads/{parent}:refs/remotes/{settings.remote}/{parent}"
    with _hook_lock(lock, settings.lock_timeout_s):
        res = _git(
            ["fetch", "--quiet", "--no-tags", settings.remote, refspec],
            cwd=repo_root,
            timeout_s=settings.git_timeout_s,
        )
    if res.returncode != 0:
        return f"git fetch {settings.remote} {parent} in {repo_root}: {res.message}"
    return None


def _result(
    cand: _Candidate,
    outcome: Outcome,
    *,
    reason: KeepReason | None = None,
    detail: str = "",
    forced: bool = False,
    discarded: tuple[str, ...] = (),
    branch_deleted: bool | None = None,
) -> ReclaimResult:
    return ReclaimResult(
        task_id=cand.task_id,
        working_dir=str(cand.working_dir),
        outcome=outcome,
        branch=cand.branch,
        reason=reason,
        detail=detail,
        forced=forced,
        discarded=discarded,
        branch_deleted=branch_deleted,
    )


def _check_merged(cand: _Candidate, settings: WorktreeReclaimSettings) -> ReclaimResult | None:
    """Keep ``cand`` unless its branch is an ancestor of the fetched parent."""
    merged_into = f"{settings.remote}/{settings.parent_branch}"
    ancestry = _git(
        ["merge-base", "--is-ancestor", f"refs/heads/{cand.branch}", f"refs/remotes/{merged_into}"],
        cwd=cand.repo_root,
        timeout_s=settings.git_timeout_s,
    )
    if ancestry.returncode == 0:
        return None
    if ancestry.returncode == 1:
        return _result(
            cand,
            Outcome.KEPT,
            reason=KeepReason.UNMERGED,
            detail=f"{cand.branch} is not an ancestor of {merged_into}",
        )
    return _result(
        cand,
        Outcome.KEPT,
        reason=KeepReason.GIT_ERROR,
        detail=f"git merge-base: {ancestry.message}",
    )


def _check_clean(
    cand: _Candidate, settings: WorktreeReclaimSettings
) -> ReclaimResult | tuple[str, ...]:
    """Keep ``cand`` if it holds work; else return the discardable paths."""
    try:
        blocking, discardable = _worktree_dirt(cand.working_dir, settings)
    except _ProbeError as exc:
        return _result(cand, Outcome.KEPT, reason=KeepReason.GIT_ERROR, detail=str(exc))
    if blocking:
        return _result(cand, Outcome.KEPT, reason=KeepReason.DIRTY, detail=_describe_dirt(blocking))
    for rel in cand.deliverables:
        if not (cand.working_dir / rel).exists():
            continue
        # A tracked deliverable is committed (and merged, by now); an
        # untracked one already failed the status check above. Only an
        # IGNORED one would vanish silently with the worktree.
        ignored = _git(
            ["check-ignore", "-q", "--", rel],
            cwd=cand.working_dir,
            timeout_s=settings.git_timeout_s,
        )
        if ignored.returncode == 0:
            return _result(
                cand,
                Outcome.KEPT,
                reason=KeepReason.DIRTY,
                detail=f"declared deliverable {rel} is gitignored; removal would delete it",
            )
        if ignored.returncode != 1:
            return _result(
                cand,
                Outcome.KEPT,
                reason=KeepReason.GIT_ERROR,
                detail=f"git check-ignore {rel}: {ignored.message}",
            )
    return tuple(discardable)


def _remove(
    cand: _Candidate,
    *,
    queue_dir: Path,
    settings: WorktreeReclaimSettings,
    lock: Path | None,
) -> ReclaimResult:
    """Re-verify under the hook lock, then remove the worktree and branch."""
    try:
        with _hook_lock(lock, settings.lock_timeout_s):
            # The first checks ran without the lock, possibly seconds ago:
            # the operator may have flipped the task back to pending, and the
            # hook may have handed the worktree to a new dispatch since.
            status, why = _task_status(queue_dir, cand.task_id)
            if status != RECLAIMABLE_STATUS:
                return _result(
                    cand,
                    Outcome.KEPT,
                    reason=KeepReason.STATUS,
                    detail=f"status changed to {status or why} before removal",
                )
            if not (cand.working_dir / ".git").is_file():
                return _result(
                    cand,
                    Outcome.KEPT,
                    reason=KeepReason.GONE,
                    detail="the worktree disappeared before removal",
                )
            clean = _check_clean(cand, settings)
            if isinstance(clean, ReclaimResult):
                return clean
            forced = bool(clean)
            removed = _git(
                ["worktree", "remove", *(["--force"] if forced else []), str(cand.worktree_path)],
                cwd=cand.repo_root,
                timeout_s=settings.git_timeout_s,
            )
            if removed.returncode != 0:
                return _result(
                    cand,
                    Outcome.FAILED,
                    detail=f"git worktree remove: {removed.message}",
                    forced=forced,
                    discarded=clean,
                )
            deleted = _git(
                ["branch", "-d", "--", cand.branch],
                cwd=cand.repo_root,
                timeout_s=settings.git_timeout_s,
            )
            branch_deleted = deleted.returncode == 0
            logger.info(
                "reclaimed worktree %s (task %s, branch %s %s%s)",
                cand.working_dir,
                cand.task_id,
                cand.branch,
                "deleted" if branch_deleted else "kept",
                f", discarded {len(clean)} untracked path(s)" if forced else "",
            )
            return _result(
                cand,
                Outcome.RECLAIMED,
                detail=""
                if branch_deleted
                else f"git branch -d kept the branch: {deleted.message}",
                forced=forced,
                discarded=clean,
                branch_deleted=branch_deleted,
            )
    except _LockBusy as exc:
        return _result(cand, Outcome.KEPT, reason=KeepReason.LOCK_BUSY, detail=str(exc))


def reclaim_worktrees(
    queue_dir: Path,
    settings: WorktreeReclaimSettings,
    *,
    apply: bool,
    in_flight_task_ids: Collection[str] = (),
    limit: int | None = None,
) -> ReclaimReport:
    """Reclaim (or, with ``apply=False``, report) finished tasks' worktrees.

    Parameters
    ----------
    queue_dir
        The queue root; must contain ``todo/``.
    settings
        The queue's ``[worktree_reclaim]`` section.
    apply
        ``False`` is a dry run: it fetches ``<remote>/<parent_branch>``
        (updating that one remote-tracking ref) but removes nothing.
    in_flight_task_ids
        Tasks a dispatch thread still holds; always kept.
    limit
        Stop after this many removals (or would-be removals); the rest are
        kept with reason ``limit``. ``None`` means unbounded.

    Raises
    ------
    ReclaimError
        ``queue_dir`` is not a queue, ``limit`` is not positive, or
        ``lock_file`` cannot be opened.
    """
    queue_dir = require_queue_dir(queue_dir)
    if limit is not None and limit < 1:
        raise ReclaimError(f"limit must be at least 1, got {limit}")

    entries, unparseable, scanned = _task_entries(queue_dir)
    owners: dict[str, list[str]] = defaultdict(list)
    for entry in entries:
        owners[os.path.realpath(entry.working_dir)].append(entry.task_id)

    in_flight = frozenset(in_flight_task_ids)
    lock = _lock_path(queue_dir, settings)
    registries: dict[Path, list[_WorktreeRecord] | str] = {}
    results: list[ReclaimResult] = []
    by_repo: dict[Path, list[_Candidate]] = defaultdict(list)

    for entry in sorted(entries, key=lambda e: (e.task_id, str(e.working_dir))):
        if not (entry.working_dir / ".git").exists():
            continue  # nothing checked out: never dispatched, or already reclaimed
        verdict = _screen(
            entry,
            queue_dir=queue_dir,
            settings=settings,
            in_flight=in_flight,
            owners=owners,
            registries=registries,
        )
        if isinstance(verdict, ReclaimResult):
            results.append(verdict)
        else:
            by_repo[verdict.repo_root].append(verdict)

    errors: list[str] = []
    attempts = 0
    for repo_root, candidates in by_repo.items():
        try:
            fetch_error = _fetch(repo_root, settings, lock)
        except _LockBusy as exc:
            results.extend(
                _result(c, Outcome.KEPT, reason=KeepReason.LOCK_BUSY, detail=str(exc))
                for c in candidates
            )
            continue
        if fetch_error is not None:
            errors.append(fetch_error)
            results.extend(
                _result(c, Outcome.KEPT, reason=KeepReason.FETCH_FAILED, detail=fetch_error)
                for c in candidates
            )
            continue
        for cand in candidates:
            unmerged = _check_merged(cand, settings)
            if unmerged is not None:
                results.append(unmerged)
                continue
            if limit is not None and attempts >= limit:
                results.append(
                    _result(
                        cand,
                        Outcome.KEPT,
                        reason=KeepReason.LIMIT,
                        detail=f"this pass already reached its limit of {limit}",
                    )
                )
                continue
            clean = _check_clean(cand, settings)
            if isinstance(clean, ReclaimResult):
                results.append(clean)
                continue
            attempts += 1
            if apply:
                results.append(_remove(cand, queue_dir=queue_dir, settings=settings, lock=lock))
            else:
                results.append(
                    _result(cand, Outcome.WOULD_RECLAIM, forced=bool(clean), discarded=clean)
                )

    results.sort(key=lambda r: (r.task_id, r.working_dir))
    return ReclaimReport(
        applied=apply,
        remote=settings.remote,
        parent_branch=settings.parent_branch,
        tasks_scanned=scanned,
        results=tuple(results),
        unparseable_tasks=tuple(unparseable),
        errors=tuple(errors),
    )
