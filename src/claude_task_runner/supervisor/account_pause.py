"""Operator pause flags for accounts, kept as marker files.

``claude-task-runner account pause <name>`` creates the empty file
``<queue>/.claude_task_runner/account_paused/<name>``;
``account resume <name>`` removes it. The supervisor reads these files
every tick (:func:`refresh`) and copies the set into its in-memory
snapshot, from which dispatch (``runner.account_dispatch.choose_account``)
and ``supervisor.json`` take ``AccountState.paused``.

The flag cannot live in ``supervisor.json`` itself: a running supervisor
rewrites that file from its own memory after every tick, so a value
another process wrote there is overwritten within one tick. Markers are
written by the CLI, and by the supervisor only once per queue
(:func:`adopt_snapshot_flags`). A marker is a presence flag with no
content, and its name is an account name (``config.schema.ACCOUNT_NAME_RE``:
no ``/``, no leading ``.``), so it never collides with the dot-prefixed
sentinel.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

from claude_task_runner.config.schema import ACCOUNT_NAME_RE
from claude_task_runner.supervisor.states import SupervisorSnapshot

__all__ = [
    "adopt_snapshot_flags",
    "apply",
    "marker_path",
    "pause_dir",
    "paused_names",
    "refresh",
    "set_paused",
]

logger = logging.getLogger(__name__)

_ADOPTED_SENTINEL = ".adopted"


def pause_dir(queue_dir: Path) -> Path:
    """Resolve ``<queue>/.claude_task_runner/account_paused/`` (no mkdir)."""
    return queue_dir / ".claude_task_runner" / "account_paused"


def marker_path(queue_dir: Path, name: str) -> Path:
    """The marker file whose presence pauses account ``name``.

    Raises ``ValueError`` for a name that is not a valid account name,
    which keeps every marker inside :func:`pause_dir`.
    """
    if not ACCOUNT_NAME_RE.match(name):
        raise ValueError(f"not an account name: {name!r}")
    return pause_dir(queue_dir) / name


def paused_names(queue_dir: Path) -> frozenset[str]:
    """Names of the accounts the operator has paused.

    Empty when the directory does not exist (nothing was ever paused).
    Any other error reading it propagates: a caller must not mistake an
    unreadable directory for "nothing is paused".
    """
    try:
        entries = list(pause_dir(queue_dir).iterdir())
    except FileNotFoundError:
        return frozenset()
    return frozenset(p.name for p in entries if p.is_file() and not p.name.startswith("."))


def set_paused(queue_dir: Path, name: str, *, paused: bool) -> bool:
    """Create (``paused=True``) or remove the marker for ``name``.

    Returns whether anything changed, so a repeated pause or resume is a
    no-op the caller can report.
    """
    target = marker_path(queue_dir, name)
    if not paused:
        try:
            target.unlink()
        except FileNotFoundError:
            return False
        return True
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.touch(exist_ok=False)
    except FileExistsError:
        return False
    return True


def apply(snapshot: SupervisorSnapshot, paused: Iterable[str]) -> SupervisorSnapshot:
    """Return ``snapshot`` with each account's ``paused`` set from ``paused``.

    An account is paused exactly when its name is in ``paused``. Names
    with no account row are ignored. ``snapshot`` itself is returned when
    no flag changes.
    """
    wanted = frozenset(paused)
    changed = {
        name: state.model_copy(update={"paused": name in wanted})
        for name, state in snapshot.accounts.items()
        if state.paused != (name in wanted)
    }
    if not changed:
        return snapshot
    return snapshot.model_copy(update={"accounts": {**snapshot.accounts, **changed}})


def refresh(queue_dir: Path, snapshot: SupervisorSnapshot) -> SupervisorSnapshot:
    """:func:`apply` the markers currently in ``queue_dir`` to ``snapshot``.

    When the marker directory cannot be read, ``snapshot`` keeps the flags
    it has and the error is logged: reading it as "nothing paused" would
    dispatch on accounts the operator stopped.
    """
    try:
        paused = paused_names(queue_dir)
    except OSError as exc:
        logger.error(
            "cannot read account pause markers in %s (%s); keeping the current pause flags",
            pause_dir(queue_dir),
            exc,
        )
        return snapshot
    return apply(snapshot, paused)


def adopt_snapshot_flags(queue_dir: Path, snapshot: SupervisorSnapshot) -> list[str]:
    """Write a marker for every account ``snapshot`` records as paused, once.

    For a supervisor's first start on a queue whose ``supervisor.json``
    holds pauses that have no markers, so the first :func:`refresh` keeps
    them. A sentinel file records that this ran: from then on
    ``supervisor.json`` only echoes the markers, and adopting its flags
    again would re-pause an account the operator resumed while no
    supervisor ran. Returns the names that got a new marker.
    """
    sentinel = pause_dir(queue_dir) / _ADOPTED_SENTINEL
    if sentinel.exists():
        return []
    adopted: list[str] = []
    for name, state in sorted(snapshot.accounts.items()):
        if not state.paused:
            continue
        if not ACCOUNT_NAME_RE.match(name):
            logger.warning("not adopting the pause of %r: not an account name", name)
            continue
        if set_paused(queue_dir, name, paused=True):
            adopted.append(name)
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()
    return adopted
