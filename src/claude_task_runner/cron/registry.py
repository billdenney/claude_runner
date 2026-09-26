"""Registry of the queue that the cron watchdog manages.

``~/.claude_task_runner/queues.json`` holds
``{"queues": ["/path/to/queue"]}``. The crontab line that a cron
``install`` adds runs ``watchdog tick`` with no ``--queue``, and a tick
restarts the registered queue's supervisor when it is not running.

Only one supervisor runs per user, because each takes the per-user
``global.lock``, so the watchdog manages one queue, as the systemd unit
runs one. A cron ``install`` registers the queue directory it was
invoked with, replacing the queue registered before, and
``watchdog register`` does the same without re-running ``install``.
While the replaced queue's supervisor still holds the lock, ticks start
none and count no restart; :func:`handover_note` says so, and how to
hand over. ``watchdog unregister`` removes a queue. A systemd
``install`` leaves the registry alone, because systemd restarts the
unit's supervisor itself, and ``install uninstall`` leaves it alone too.

A registry written before ``register`` replaced its entry can list
several queues. :func:`managed_queue` picks the last, the most recently
registered; a tick ignores the others (:func:`ignored_queues`) and warns
about them, and the next ``install`` or ``register`` rewrites the file
to one entry. A tick skips a managed queue that is no longer an existing
directory and logs an ERROR for it, but leaves it registered.

There are two readers. :func:`read_registered_queues` raises
:class:`RegistryError` on a corrupt file and writes nothing, for callers
that must tell an empty registry from a broken one, such as ``doctor``
and :func:`unregister_queue`. :func:`load_registered_queues` logs a
corrupt file, keeps a copy as ``queues.json.broken`` and returns ``[]``,
so a watchdog tick keeps running.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

from claude_task_runner.queue.store import require_queue_dir
from claude_task_runner.supervisor import pidfile as pidfile_mod

logger = logging.getLogger(__name__)

QUEUES_REGISTRY_FILENAME = "queues.json"
"""Stored under ``~/.claude_task_runner/``, so each user has one registry."""


class RegistryError(ValueError):
    """``queues.json`` exists but does not hold a registry."""


def queues_registry_path() -> Path:
    """Resolve ``~/.claude_task_runner/queues.json``."""
    return Path.home() / ".claude_task_runner" / QUEUES_REGISTRY_FILENAME


def read_registered_queues() -> list[Path]:
    """Return the registered queues, or ``[]`` when there is no registry file.

    Raises :class:`RegistryError` when the file exists but cannot be
    read, is not JSON, is not a JSON object, or has a ``queues`` value
    that is not a list. Writes nothing."""
    path = queues_registry_path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"corrupt queues registry at {path} ({exc})") from exc
    if not isinstance(payload, dict):
        raise RegistryError(f"queues registry at {path} is not a JSON object")
    raw = payload.get("queues", [])
    if not isinstance(raw, list):
        raise RegistryError(f"queues registry at {path}: 'queues' is not a list")
    return [Path(q) for q in raw if isinstance(q, str)]


def _backup_broken_registry(path: Path) -> None:
    """Preserve a corrupt registry as ``<name>.broken`` before it's lost.

    Best-effort: a failure to back up must not crash the watchdog tick
    (the registry is already unreadable; losing the backup is a smaller
    problem than aborting the tick)."""
    backup = path.with_suffix(path.suffix + ".broken")
    try:
        shutil.copy2(path, backup)
    except OSError as exc:
        logger.error("watchdog: could not back up corrupt registry to %s (%s)", backup, exc)
    else:
        logger.error("watchdog: backed up corrupt registry to %s", backup)


def load_registered_queues() -> list[Path]:
    """Return the registered queues, treating a corrupt registry as empty.

    A corrupt registry would otherwise silently lose every queue
    registration, so it is logged at ERROR and copied to
    ``queues.json.broken``, where the operator can recover it before the
    next ``register`` overwrites it."""
    try:
        return read_registered_queues()
    except RegistryError as exc:
        logger.error("watchdog: %s", exc)
        _backup_broken_registry(queues_registry_path())
        return []


def _write_registry(queues: list[Path]) -> None:
    """Replace the registry with ``queues``.

    Writes a temporary file and renames it over the registry, so a tick
    reading it at the same moment sees the old list or the new one,
    never half a file. On failure the registry is unchanged and the
    temporary file is removed."""
    path = queues_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(json.dumps({"queues": [str(q) for q in queues]}, indent=2) + "\n")
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise


def managed_queue(queues: list[Path]) -> Path | None:
    """Return the queue the cron watchdog manages: the last one in ``queues``.

    ``queues`` is the registry as read. The last entry is the most recently
    registered, so in a registry that lists several it is the queue a later
    ``install`` or ``register`` meant. ``None`` for an empty registry."""
    return queues[-1] if queues else None


def ignored_queues(queues: list[Path]) -> list[Path]:
    """Return the other queues that ``queues`` lists, which the watchdog ignores.

    Each appears once, in registry order. Only a registry written before
    :func:`register_queue` replaced its entry has any."""
    managed = managed_queue(queues)
    return [q for q in dict.fromkeys(queues) if q != managed]


def register_queue(queue_dir: Path) -> list[Path]:
    """Make ``queue_dir`` the one queue the cron watchdog manages.

    Returns the queues it replaced, each once and in registry order, or
    ``[]`` when ``queue_dir`` was already the only entry. The watchdog
    manages one queue (see the module docstring), so this replaces the
    queue registered before, and every entry of a registry that lists
    several.

    Raises :class:`NotADirectoryError` unless ``queue_dir`` is an
    existing directory (see :func:`require_queue_dir`). A registered typo
    would otherwise be created by the next tick's restart, and the
    supervisor started on that empty queue would hold the per-user
    global lock. The tick checks the path again, because a registered
    queue can be deleted or moved later."""
    resolved = require_queue_dir(queue_dir)
    existing = load_registered_queues()
    if existing == [resolved]:
        return []
    _write_registry([resolved])
    return [q for q in dict.fromkeys(existing) if q != resolved]


def _recorded_supervisor_pid(queue: Path) -> int | None:
    """Return the PID in ``<queue>/.claude_task_runner/supervisor.pid``, if any."""
    return pidfile_mod.read_existing_pid(queue / ".claude_task_runner" / "supervisor.pid")


def handover_note(queue: Path, replaced: list[Path]) -> str | None:
    """Say why a tick cannot start ``queue``'s supervisor yet, if it cannot.

    ``None`` when ``global.lock`` is free, so the next tick starts it, or
    when ``queue``'s own supervisor holds the lock. Otherwise ticks start
    none, and count no restart, until the holder exits. When the holder is
    the supervisor of a queue in ``replaced``, the note gives the command
    that hands over now: ``supervisor drain`` lets that queue's in-flight
    tasks finish, then exits."""
    try:
        probe = pidfile_mod.probe_global_lock()
    except OSError as exc:
        return f"Could not check whether another supervisor holds global.lock: {exc}"
    if not probe.held:
        return None
    if probe.pid is None:
        return (
            "Another supervisor holds global.lock, so the watchdog starts this "
            "queue's supervisor once it exits."
        )
    if _recorded_supervisor_pid(queue) == probe.pid:
        return None
    for old in replaced:
        if _recorded_supervisor_pid(old) == probe.pid:
            return (
                f"The supervisor for {old} (pid {probe.pid}) still holds global.lock, "
                "so the watchdog starts this queue's supervisor once it exits. To hand "
                f"over now, run: claude-task-runner supervisor drain --queue {old}"
            )
    return (
        f"Another supervisor (pid {probe.pid}) holds global.lock, so the watchdog "
        "starts this queue's supervisor once it exits."
    )


def unregister_queue(queue_dir: Path) -> list[Path]:
    """Remove ``queue_dir`` from the registry. Return the entries removed.

    Idempotent: when the queue is not listed, or there is no registry
    file, nothing is written and the result is ``[]``. The directory
    need not exist, since dropping a queue that was deleted or moved is
    the main use. An entry matches when it equals ``queue_dir`` made
    absolute or resolved. The first form matches a path copied from
    ``watchdog queues`` or from ``watchdog.log`` even when a symlink on
    it has changed since it was registered. Every matching entry goes.

    Raises :class:`RegistryError` on a corrupt registry and leaves the
    file as it was. The lenient :func:`load_registered_queues` would
    read it as empty, and rewriting that would drop every other queue."""
    targets = {Path(os.path.abspath(queue_dir)), queue_dir.resolve()}
    registered = read_registered_queues()
    removed = [q for q in registered if q in targets]
    if removed:
        _write_registry([q for q in registered if q not in targets])
    return removed
