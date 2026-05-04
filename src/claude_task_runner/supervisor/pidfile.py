"""Single-supervisor PID file enforcement.

Architectural invariant 1 (``docs/architecture.md``): **at most one
supervisor process per host**. We enforce by:

1. Acquiring an exclusive ``fcntl.flock`` on
   ``~/.claude_task_runner/global.lock``. The OS releases the lock
   automatically when the holder process exits (clean shutdown,
   crash, or kill).
2. Writing the supervisor's PID into the locked file so other tools
   (the watchdog, ``doctor``) can read it.

Per-queue ``supervisor.pid`` files are also maintained so multiple
tooling consumers can find the live PID without holding the lock
themselves.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

GLOBAL_LOCK_FILENAME = "global.lock"
"""Stored under ``~/.claude_task_runner/`` so it's per-user, not
per-queue. A user with multiple queues still gets a single
supervisor across them."""


class SupervisorAlreadyRunning(RuntimeError):
    """Another supervisor process holds ``global.lock``.

    ``existing_pid`` is the PID we found in the lock file (best-effort —
    may be ``None`` if the file was empty or unreadable).
    """

    def __init__(self, lock_path: Path, existing_pid: int | None) -> None:
        self.lock_path = lock_path
        self.existing_pid = existing_pid
        msg = f"another supervisor is already running ({lock_path})"
        if existing_pid is not None:
            msg += f"; pid={existing_pid}"
        super().__init__(msg)


def global_lock_dir() -> Path:
    """Per-user lock directory: ``~/.claude_task_runner/``.

    Created if it doesn't exist.
    """
    base = Path.home() / ".claude_task_runner"
    base.mkdir(parents=True, exist_ok=True)
    return base


def global_lock_path() -> Path:
    """Path to the host-wide ``global.lock`` file."""
    return global_lock_dir() / GLOBAL_LOCK_FILENAME


def read_existing_pid(path: Path) -> int | None:
    """Best-effort: read the PID written into a lock file."""
    if not path.exists():
        return None
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def is_pid_alive(pid: int) -> bool:
    """Cheap liveness check via ``os.kill(pid, 0)``."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we can't signal it (different user). Still alive.
        return True
    return True


@contextmanager
def acquire_global_lock(*, lock_path: Path | None = None) -> Iterator[Path]:
    """Context manager: acquire the host-wide supervisor lock.

    Writes the current PID into the lock file. Releases the lock on
    context exit (the OS would also release it on crash). Raises
    :class:`SupervisorAlreadyRunning` if another process holds it.

    Usage::

        with acquire_global_lock():
            run_supervisor_loop()
    """
    path = lock_path if lock_path is not None else global_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    fh: IO[str] = path.open("a+")
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            existing = read_existing_pid(path)
            fh.close()
            raise SupervisorAlreadyRunning(path, existing) from exc

        # Truncate and write our PID.
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        os.fsync(fh.fileno())

        try:
            yield path
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        with contextlib.suppress(OSError):
            fh.close()


def write_pid_file(path: Path) -> None:
    """Best-effort PID write for telemetry consumers (watchdog, doctor).

    Distinct from the global lock: this PID file is per-queue
    (``<queue>/.claude_task_runner/supervisor.pid``) and not used for
    mutual exclusion. The global lock is the source of truth.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{os.getpid()}\n")


def clear_pid_file(path: Path) -> None:
    """Remove a per-queue supervisor.pid file. Idempotent."""
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return
