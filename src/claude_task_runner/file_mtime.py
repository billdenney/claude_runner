"""A file's modification time as an aware UTC datetime, out-of-range ones
reported once.

tmpfs stores an mtime past year 9999 as given, and no datetime can hold
one. The silent-orphan reaper reads worker logs' and worktree files'
mtimes on every pass, so a WARNING for each such mtime would repeat every
pass; it is given once per process for each file, mtime and consequence
(:func:`claude_task_runner.once.first_time`).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

from claude_task_runner.clock import TimestampOutOfRange, utc_from_timestamp
from claude_task_runner.once import first_time

logger = logging.getLogger(__name__)


def file_mtime_utc(path: str | os.PathLike[str], *, consequence: str) -> datetime | None:
    """``path``'s mtime as an aware UTC datetime, or ``None``.

    ``None`` without a word when ``path`` cannot be stat'ed, and for an
    mtime no datetime can hold, with a WARNING ending in ``consequence``
    (what the caller does instead). That WARNING is given once per process
    for each path, mtime and consequence, so two callers that do different
    things with the same file each give theirs.
    """
    try:
        mtime = os.stat(path).st_mtime
    except (OSError, ValueError):
        # ValueError: a NUL byte in the path, which no file has.
        return None
    try:
        return utc_from_timestamp(mtime)
    except TimestampOutOfRange as exc:
        if first_time(("file_mtime_utc", consequence, os.fspath(path), mtime)):
            logger.warning(
                "%s has an mtime no datetime can hold (%s); %s", os.fspath(path), exc, consequence
            )
        return None
