"""Operator pause flags for accounts, kept as marker files the CLI owns.

``claude-task-runner account pause <name>`` creates
``<queue>/.claude_task_runner/account_paused/<name>``;
``account resume <name>`` removes it. The supervisor reads these files
every tick (:func:`refresh`) and copies the set into its in-memory
snapshot, from which dispatch (``runner.account_dispatch.choose_account``)
and ``supervisor.json`` take ``AccountState.paused``. It writes a marker
only at startup, for a pause ``supervisor.json`` already records
(:func:`adopt_snapshot_flags`).

The flag cannot live in ``supervisor.json`` itself: a running supervisor
rewrites that file from its own memory after every tick, so a value
another process wrote there is overwritten within one tick. One writer
per file avoids that. Account names match ``_ACCOUNT_NAME_RE`` in
``config.schema`` (no ``/``, no leading ``.``), so a name is a safe file
name and never collides with the dot-prefixed temporary files.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from claude_task_runner.queue.sidecar import _write_json_atomic
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


def pause_dir(queue_dir: Path) -> Path:
    """Resolve ``<queue>/.claude_task_runner/account_paused/`` (no mkdir)."""
    return queue_dir / ".claude_task_runner" / "account_paused"


def marker_path(queue_dir: Path, name: str) -> Path:
    """The marker file whose presence pauses account ``name``."""
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


def set_paused(queue_dir: Path, name: str, *, paused: bool, now: datetime | None = None) -> bool:
    """Create (``paused=True``) or remove the marker for ``name``.

    Returns whether anything changed, so a repeated pause or resume is a
    no-op the caller can report. The marker is written atomically and
    records when the pause was set.
    """
    target = marker_path(queue_dir, name)
    if not paused:
        try:
            target.unlink()
        except FileNotFoundError:
            return False
        return True
    if target.is_file():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(
        target, {"account": name, "paused_at": (now or datetime.now(UTC)).isoformat()}
    )
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


def adopt_snapshot_flags(queue_dir: Path, snapshot: SupervisorSnapshot) -> list[str]:
    """Write a marker for every account ``snapshot`` records as paused.

    For a supervisor starting on a ``supervisor.json`` whose ``paused``
    flags were set without markers, so those pauses survive the first
    :func:`apply`. Returns the names that got a new marker.
    """
    return [
        name
        for name, state in sorted(snapshot.accounts.items())
        if state.paused and set_paused(queue_dir, name, paused=True)
    ]


def refresh(snapshot: SupervisorSnapshot, queue_dir: Path) -> SupervisorSnapshot:
    """:func:`apply` the markers currently in ``queue_dir`` to ``snapshot``.

    When the marker directory cannot be read, ``snapshot`` keeps the flags
    it has and the error is logged: reading it as "nothing paused" would
    dispatch on accounts the operator stopped.
    """
    try:
        paused = paused_names(queue_dir)
    except OSError:
        logger.exception(
            "cannot read account pause markers in %s; keeping the current pause flags",
            pause_dir(queue_dir),
        )
        return snapshot
    return apply(snapshot, paused)
