"""The supervisor's own user, and whether an account's ``linux_user`` can be reached.

An account with a ``linux_user`` other than the supervisor's own runs its
workers through ``sudo -n -u <linux_user>``. :func:`sudo_problem` is the
check :func:`config.loader.resolve_accounts` applies to every account, so
an account whose ``linux_user`` cannot be reached takes no tasks, as one
whose ``runner-account.toml`` does not load; the dispatcher checks again
when it builds the worker's command line.
"""

from __future__ import annotations

import logging
import os
import shutil

logger = logging.getLogger(__name__)


def current_username() -> str:
    """The supervisor's own Linux username; empty when it cannot be looked up.

    A failed lookup (an unmapped uid in a container) is logged at DEBUG, and
    then every ``linux_user`` counts as another user's.
    """
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    except Exception as exc:
        logger.debug("could not resolve current username: %s", exc)
        return ""


def needs_sudo(linux_user: str | None) -> bool:
    """Whether a worker for ``linux_user`` runs through ``sudo``."""
    return bool(linux_user) and linux_user != current_username()


def sudo_problem(linux_user: str | None) -> str | None:
    """Why a worker for ``linux_user`` cannot be started, or ``None``.

    It needs ``sudo`` and ``sudo`` is not on PATH. Whether ``sudo -n``
    then admits the supervisor's user is the doctor's ``account_sudo``
    check, since probing it runs a command.
    """
    if needs_sudo(linux_user) and shutil.which("sudo") is None:
        return f"linux_user={linux_user!r} requested but sudo not on PATH"
    return None
