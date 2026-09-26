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

The PID stays in ``global.lock`` after its holder exits, and the OS may
later give that number to an unrelated process, so the file's content
cannot say whether a supervisor is running. :func:`probe_global_lock`
asks the lock itself.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, NamedTuple

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


class GlobalLockProbe(NamedTuple):
    """What :func:`probe_global_lock` found."""

    held: bool
    """A process holds the lock, so a supervisor started now would exit
    with :class:`SupervisorAlreadyRunning`."""

    pid: int | None
    """The PID written in the lock file while it is ``held``: the holder's,
    since :func:`acquire_global_lock` writes it right after locking.
    ``None`` when the lock is free, or when the file holds no PID yet."""


def probe_global_lock(*, lock_path: Path | None = None) -> GlobalLockProbe:
    """Report whether a process holds ``global.lock``, without keeping it.

    Tries a shared, non-blocking ``flock`` and releases it at once. The
    OS drops a flock when its holder exits, so unlike the PID left in the
    file, a lock that is free is never mistaken for a running supervisor.
    A supervisor that tries to lock the file during the few microseconds
    the probe holds it fails as if another supervisor were running.

    A missing lock file counts as free, and the probe does not create it.
    Raises :class:`OSError` when the file exists but cannot be opened, or
    when ``flock`` fails for a reason other than the lock being held.
    """
    path = lock_path if lock_path is not None else global_lock_path()
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return GlobalLockProbe(held=False, pid=None)
    try:
        try:
            # Shared, so a read-only descriptor can take it on filesystems
            # that emulate flock with fcntl locks, such as NFS.
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return GlobalLockProbe(held=True, pid=read_existing_pid(path))
        fcntl.flock(fd, fcntl.LOCK_UN)
        return GlobalLockProbe(held=False, pid=None)
    finally:
        os.close(fd)


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
