"""Atomic JSON persistence for :class:`SupervisorSnapshot`.

Mirror of :mod:`queue.store`'s atomic-write pattern: tempfile +
``os.replace`` so a concurrent reader (the watchdog) always sees a
complete file.

Stored at ``<queue>/.claude_task_runner/supervisor.json`` per
``[supervisor].state_file``.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from claude_task_runner.queue.schema import CURRENT_SCHEMA_VERSION
from claude_task_runner.queue.store import queue_runtime_dir
from claude_task_runner.supervisor.states import SupervisorSnapshot, SupervisorState


class SupervisorPersistenceError(ValueError):
    """Reading or writing ``supervisor.json`` failed."""


def supervisor_state_path(queue_dir: Path, state_file: str = "supervisor.json") -> Path:
    """Resolve ``<queue>/.claude_task_runner/<state_file>``."""
    return queue_runtime_dir(queue_dir) / state_file


def load(path: Path) -> SupervisorSnapshot | None:
    """Read a persisted snapshot, or ``None`` if the file doesn't exist.

    Raises :class:`SupervisorPersistenceError` if the file exists but
    can't be parsed — the daemon treats that as "fail loudly" rather
    than silently overwriting potentially-recoverable state.
    """
    if not path.exists():
        return None
    try:
        with path.open("rb") as fh:
            payload = json.load(fh)
    except OSError as exc:
        raise SupervisorPersistenceError(f"failed to read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SupervisorPersistenceError(f"{path}: invalid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise SupervisorPersistenceError(f"{path}: top-level JSON must be an object")

    sv = payload.get("schema_version", CURRENT_SCHEMA_VERSION)
    if sv != CURRENT_SCHEMA_VERSION:
        raise SupervisorPersistenceError(
            f"{path}: schema_version={sv} does not match supported {CURRENT_SCHEMA_VERSION}"
        )

    try:
        return SupervisorSnapshot.model_validate(payload)
    except ValidationError as exc:
        raise SupervisorPersistenceError(f"{path}: {exc}") from exc


def write_atomic(snapshot: SupervisorSnapshot, path: Path) -> None:
    """Atomic JSON write of the supervisor snapshot."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    payload = snapshot.model_dump(mode="json")
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=parent,
        delete=False,
        prefix=f".{path.name}.",
        suffix=".tmp",
    ) as tmp:
        json.dump(payload, tmp, indent=2, sort_keys=True, default=str)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def initial_snapshot(*, since: datetime) -> SupervisorSnapshot:
    """Build a fresh snapshot for first-time supervisor start.

    Begins in ``IDLE`` so the next clean reading drives the first real
    classification.
    """
    return SupervisorSnapshot(
        state=SupervisorState.IDLE,
        since=since,
    )
