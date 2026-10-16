"""Expanding a leading ``~`` in a configured path, with a typed error.

``Path.expanduser`` raises :class:`RuntimeError` when a leading ``~user``
names no user, and opening a path with a NUL byte raises
:class:`ValueError`. Neither belongs in code that must keep running, such
as the supervisor's tick loop or a dispatch thread. So this module is the
one place that calls ``expanduser`` (``tests/unit/test_expanduser_gate.py``
enforces that), and it raises a :class:`PathExpansionError` subclass
instead, which each caller turns into its own error:

* :func:`expand_config_dir` for an account's ``config_dir``
  (:class:`ConfigDirError`). The queue TOML refuses one that cannot be
  expanded when it loads; later it fails only that account.
* :func:`expand_task_path` for a task's ``working_dir`` or
  ``additional_dirs`` entry, or ``[worktree_reclaim].lock_file``
  (:class:`TaskPathError`). A dispatch parks only that task, ``--add-dir``
  skips the entry, a reclaim pass skips the task, and the lock file is
  refused when the queue TOML loads.
"""

from __future__ import annotations

from pathlib import Path


class PathExpansionError(ValueError):
    """A path whose leading ``~user`` names no user, or that holds a NUL byte."""


class ConfigDirError(PathExpansionError):
    """An account's ``config_dir`` that cannot be expanded."""


class TaskPathError(PathExpansionError):
    """A task path, or another path from the queue's settings, that cannot be expanded."""


def _expand(path: str | Path, what: str, error: type[PathExpansionError]) -> Path:
    text = str(path)
    if "\0" in text:
        raise error(f"{what} {text!r} contains a NUL byte")
    try:
        return Path(text).expanduser()
    except RuntimeError as exc:
        raise error(f"{what} {text!r} cannot be expanded: {exc}") from exc


def expand_config_dir(config_dir: str) -> Path:
    """``config_dir`` with a leading ``~`` or ``~user`` expanded.

    Raises :class:`ConfigDirError`, naming ``config_dir``, when it cannot
    be expanded or holds a NUL byte.
    """
    return _expand(config_dir, "config_dir", ConfigDirError)


def expand_task_path(path: str | Path, what: str) -> Path:
    """``path`` with a leading ``~`` or ``~user`` expanded.

    ``what`` names the path in the error, e.g. ``"working_dir"``. Raises
    :class:`TaskPathError` when it cannot be expanded or holds a NUL byte.
    """
    return _expand(path, what, TaskPathError)
