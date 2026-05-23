"""Per-account long-lived OAuth token (``claude setup-token``) file.

PR 1-13 read the OAuth bearer from ``<config_dir>/.credentials.json``
— the file Claude Code writes during ``claude /login``. That file's
``accessToken`` is short-lived (~24h) and refreshed by the CLI on
every successful API call. If the access token expires AND the
refresh token is revoked (e.g. a parallel login on another device
rotated it), the supervisor has no path back to a valid bearer
without operator intervention.

PR 14 adds a second authentication channel: a long-lived token
minted by ``claude setup-token`` (~1-year lifetime, "inference-only"
scope, returned as a single ``sk-ant-oat01-…`` string). The operator
runs ``setup-token`` once per account, drops the resulting string
into ``<config_dir>/oauth-token`` (one line, no JSON wrapper, mode
0600), and the supervisor uses it for:

  * ``ApiUsageSource``'s ``/v1/messages`` probe (PR 6) — instead of
    the short-lived ``accessToken`` from ``.credentials.json``.
  * ``CLAUDE_CODE_OAUTH_TOKEN`` env var on every dispatched
    ``claude`` subprocess — the CLI honors this env var as the
    auth bearer, bypassing ``.credentials.json`` entirely (the same
    integration pattern documented for GitHub Actions).

When the long-lived file is absent the runner falls back to the
``.credentials.json`` path verbatim, so single-account / pre-PR-14
deployments are unchanged.

File format
-----------
* Path: ``<config_dir>/oauth-token`` (relative to the account's
  ``CLAUDE_CONFIG_DIR``).
* Contents: exactly one line — the raw token string. Leading and
  trailing whitespace are stripped on read; empty/whitespace-only
  files are treated as "not present" so a half-written file doesn't
  produce a 401 storm.
* Permissions: not enforced by the runner — the file holds a bearer
  and ``chmod 600`` is the operator's responsibility. The runner
  logs a one-line warning at startup if it finds the file
  world-readable, mirroring the heuristic Claude Code uses for
  ``.credentials.json``.

Why a file (not an env var)?
----------------------------
A per-account file in ``<config_dir>`` keeps the token co-located
with the other account artifacts (``.credentials.json``,
``runner-account.toml``) and isolates accounts from each other —
``CLAUDE_CODE_OAUTH_TOKEN_WORK`` vs ``…_PERSONAL`` would force the
supervisor to know each account's "env var name", which complicates
the multi-Linux-user dispatch path (PR 3) where the spawned ``claude``
runs under a different uid. Per-account file paths follow the
existing ``CLAUDE_CONFIG_DIR``-rooted convention with no new
configuration surface.
"""

from __future__ import annotations

import logging
import stat
from pathlib import Path

logger = logging.getLogger(__name__)

OAUTH_TOKEN_FILENAME = "oauth-token"
"""File name under each account's ``CLAUDE_CONFIG_DIR``."""


def oauth_token_path(config_dir: str) -> Path:
    """Return the absolute path to an account's ``oauth-token`` file.

    Empty ``config_dir`` targets ``~/.claude`` to match the
    convention used by :mod:`claude_task_runner.usage.api_source`.
    """
    base = Path(config_dir).expanduser() if config_dir else Path.home() / ".claude"
    return base / OAUTH_TOKEN_FILENAME


def read_long_lived_token(config_dir: str) -> str | None:
    """Return the long-lived bearer string, or ``None`` if not configured.

    Returns ``None`` for any of:

    * The file does not exist.
    * The file exists but is empty / whitespace-only.
    * The file cannot be read (logged at warning level — the operator
      probably wants to know, but the supervisor must keep running).

    A non-``None`` return is the stripped token string with no
    surrounding whitespace and no newline.

    The function is intentionally side-effect-free except for the
    permission-mode warning: the supervisor can call it once per tick
    without burning fds or repeatedly stat-ing the file (the cost is
    one stat + small read).
    """
    path = oauth_token_path(config_dir)
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("could not read long-lived OAuth token file %s: %s", path, exc)
        return None
    token = text.strip()
    if not token:
        return None
    _warn_if_world_readable(path)
    return token


def _warn_if_world_readable(path: Path) -> None:
    """Log once if a token file is readable by group or other.

    Best-effort: ``Path.stat`` may fail under unusual filesystems; we
    swallow the error rather than crash the supervisor over a
    permission check.
    """
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    leaky = stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH
    if mode & leaky:
        logger.warning(
            "long-lived OAuth token file %s has loose permissions "
            "(mode %o); consider `chmod 600 %s`",
            path,
            stat.S_IMODE(mode),
            path,
        )
