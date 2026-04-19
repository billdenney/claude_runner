"""Per-task advisory file lock using fcntl."""

from __future__ import annotations

import contextlib
import fcntl
import os
from collections.abc import Iterator
from pathlib import Path


class TaskLockError(RuntimeError):
    pass


@contextlib.contextmanager
def task_lock(lock_path: Path) -> Iterator[None]:
    """Acquire an exclusive, non-blocking fcntl lock on `lock_path`.

    Raises TaskLockError if another process (or another runner instance in the
    same process tree) holds the lock. The lock file persists but the lock is
    released when the context exits.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise TaskLockError(f"could not acquire task lock {lock_path}: {exc}") from exc
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
