"""Expanding an account's ``config_dir``.

``Path.expanduser`` raises :class:`RuntimeError` when a leading ``~user``
names no user. The queue TOML refuses such a ``config_dir`` when it loads
(:class:`config.schema.AccountSettings`). Every caller after that goes
through :func:`expand_config_dir`, which raises :class:`ConfigDirError`
instead, so a user removed while the supervisor runs fails that account,
each caller in its own error type, rather than ending the tick loop.
"""

from __future__ import annotations

from pathlib import Path


class ConfigDirError(ValueError):
    """A ``config_dir`` that cannot be expanded: its leading ``~user`` names no user."""


def expand_config_dir(config_dir: str) -> Path:
    """``config_dir`` with a leading ``~`` or ``~user`` expanded.

    Raises :class:`ConfigDirError`, naming ``config_dir``, when it cannot
    be expanded.
    """
    try:
        return Path(config_dir).expanduser()
    except RuntimeError as exc:
        raise ConfigDirError(f"config_dir {config_dir!r} cannot be expanded: {exc}") from exc
