"""Tell the first time this process meets something from every later time.

The supervisor reloads its task YAMLs on every tick and its TOML on every
SIGHUP, and the silent-orphan reaper stats the same files on every pass, so
a WARNING about what they find would repeat each time. A caller asks
:func:`first_time` with a key naming its warning and what it is about, and
warns only on ``True``. It imports nothing from the package, so any module
can use it.
"""

from __future__ import annotations

import threading
from collections.abc import Hashable

MAX_KEYS = 10_000
"""Keys remembered at most. Past it the oldest is forgotten, so the warning
it stood for can be given once more."""

_seen: dict[Hashable, None] = {}
"""The keys met so far, oldest first."""

_lock = threading.Lock()


def first_time(key: Hashable) -> bool:
    """``True`` the first time this process asks about ``key``, ``False`` after.

    Once per process: a warning keyed on a file's state stays quiet when that
    state goes away and comes back. Start the key with a name for the
    warning, such as ``("retired_task_key", path, key)``, so that two
    warnings about the same thing do not silence each other. Safe to call
    from several threads.
    """
    with _lock:
        if key in _seen:
            return False
        if len(_seen) >= MAX_KEYS:
            del _seen[next(iter(_seen))]
        _seen[key] = None
        return True
