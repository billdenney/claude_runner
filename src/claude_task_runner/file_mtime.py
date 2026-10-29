"""A file's modification time as an aware UTC datetime, out-of-range ones
reported once.

tmpfs stores an mtime past year 9999 as given, and no datetime can hold
one. The silent-orphan reaper reads worker logs' and worktree files'
mtimes on every pass, so a WARNING for each such mtime would repeat every
pass; each ``(path, mtime)`` pair is reported once per process instead, as
``queue.store`` does for retired task keys.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

from claude_task_runner.clock import TimestampOutOfRange, utc_from_timestamp

logger = logging.getLogger(__name__)

_reported: set[tuple[str, float]] = set()
"""``(path, mtime)`` pairs :func:`report_once` has already answered."""


def report_once(path: str | os.PathLike[str], mtime: float) -> bool:
    """Whether to report that no datetime can hold ``path``'s ``mtime``:
    ``True`` the first time this process asks about the pair, then
    ``False``. A new mtime for the same path is a new pair."""
    key = (os.fspath(path), mtime)
    if key in _reported:
        return False
    _reported.add(key)
    return True


def file_mtime_utc(path: str | os.PathLike[str], *, consequence: str) -> datetime | None:
    """``path``'s mtime as an aware UTC datetime, or ``None``.

    ``None`` without a word when ``path`` cannot be stat'ed, and for an
    mtime no datetime can hold, with a WARNING ending in ``consequence``
    (what the caller does instead) the first time :func:`report_once`
    sees it.
    """
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return None
    try:
        return utc_from_timestamp(mtime)
    except TimestampOutOfRange as exc:
        if report_once(path, mtime):
            logger.warning(
                "%s has an mtime no datetime can hold (%s); %s", os.fspath(path), exc, consequence
            )
        return None
