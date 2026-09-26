"""Permission errors, injected where the kernel would raise them.

A test could chmod a directory to make it read-only or unsearchable, but
root ignores those modes, so such a test has to skip when run as root.
These helpers patch the ``os`` functions that pathlib and shutil call
instead, and raise the ``PermissionError`` the kernel raises: errno
EACCES, with the path as its filename. They work the same for every user.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest


def _denied(path: Path) -> PermissionError:
    return PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(path))


def read_only(monkeypatch: pytest.MonkeyPatch, directory: Path) -> None:
    """Make ``directory`` refuse new entries and removals, as mode 0o555 does.

    ``os.mkdir`` and ``os.symlink`` raise EACCES for a new name directly in
    ``directory``, and ``os.unlink`` for an existing one. As with the
    kernel, a name that already exists (or, for unlink, does not) gets the
    error it would get anyway.
    """
    for func, arg, exists in (("mkdir", 0, False), ("symlink", 1, False), ("unlink", 0, True)):
        real = getattr(os, func)

        def refuse(*args, _real=real, _arg=arg, _exists=exists, **kwargs):
            if len(args) > _arg:
                path = Path(os.fsdecode(args[_arg]))
                if path.parent == directory and os.path.lexists(path) is _exists:
                    raise _denied(path)
            return _real(*args, **kwargs)

        monkeypatch.setattr(os, func, refuse)


def unsearchable(monkeypatch: pytest.MonkeyPatch, directory: Path) -> None:
    """Make every path below ``directory`` impossible to stat, as mode 0o000 does.

    ``os.stat`` and ``os.lstat`` raise EACCES for any path strictly below
    ``directory``. ``directory`` itself still stats, since that needs only
    its parent. pathlib stats through ``os.stat``, and Python 3.14's
    ``Path.is_symlink`` through ``os.lstat``.
    """
    for func in ("stat", "lstat"):
        real = getattr(os, func)

        def refuse(path, *args, _real=real, **kwargs):
            if not isinstance(path, int):
                target = Path(os.fsdecode(path))
                if target != directory and target.is_relative_to(directory):
                    raise _denied(target)
            return _real(path, *args, **kwargs)

        monkeypatch.setattr(os, func, refuse)
