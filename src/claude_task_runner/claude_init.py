"""Pre-initialize a ``CLAUDE_CONFIG_DIR`` so spawned ``claude`` subprocesses
don't get stuck at first-run onboarding or per-directory trust prompts.

The interactive ``claude`` TUI (which the runner uses for ``/usage`` captures)
gates on two flags in ``<config_dir>/.claude.json``:

* ``hasCompletedOnboarding`` — when missing/False, claude shows the
  first-run theme picker before drawing the TUI's input field. The
  picker's contiguous-ASCII markers are split by ``\\x1b[1C`` cursor
  escapes, which makes it hard to dismiss reliably from pexpect.
* ``projects[<abs_dir>].hasTrustDialogAccepted`` — when missing/False
  for the spawn's CWD, claude shows the per-directory "Quick safety
  check" prompt. Same ANSI-split problem when trying to dismiss.

This module's :func:`ensure_initialized` flips both flags ahead of time.
It is idempotent (re-running it is a fast no-op once the flags are set)
and atomic (tmp + ``os.replace``), so calling it on every spawn is cheap.

See ADR notes adjacent to ``usage/capture.py`` for background.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def ensure_initialized(
    config_dir: str | Path | None,
    trust_dir: str | Path,
) -> bool:
    """Ensure ``config_dir/.claude.json`` is past onboarding and trusts ``trust_dir``.

    Parameters
    ----------
    config_dir
        ``CLAUDE_CONFIG_DIR`` to prepare. Empty / ``None`` means the
        default ``~/.claude``.
    trust_dir
        Directory that the spawned ``claude`` will run in. Resolved to
        an absolute canonical path before being used as the
        ``projects`` dict key.

    Returns
    -------
    bool
        ``True`` if the file was rewritten, ``False`` if no change was
        needed or the file/dir is not in a state we can safely touch
        (missing config, malformed JSON, unexpected schema).

    Notes
    -----
    No-op when ``.claude.json`` does not yet exist — ``claude`` itself
    creates it on first launch, and a subsequent call will flip the
    flags then.
    """
    if config_dir is None or str(config_dir) == "":
        cfg_path = Path.home() / ".claude"
    else:
        cfg_path = Path(config_dir).expanduser()
    config_file = cfg_path / ".claude.json"
    if not config_file.is_file():
        return False

    trust_key = str(Path(trust_dir).expanduser().resolve())

    try:
        data = json.loads(config_file.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("ensure_initialized: cannot parse %s: %s", config_file, exc)
        return False
    if not isinstance(data, dict):
        return False

    changed = False

    if data.get("hasCompletedOnboarding") is not True:
        data["hasCompletedOnboarding"] = True
        changed = True

    projects = data.setdefault("projects", {})
    if not isinstance(projects, dict):
        return False
    entry = projects.setdefault(trust_key, {})
    if not isinstance(entry, dict):
        return False
    if entry.get("hasTrustDialogAccepted") is not True:
        entry["hasTrustDialogAccepted"] = True
        changed = True

    if not changed:
        return False

    # Atomic write: rename onto the target preserves either old or new
    # for any concurrent reader. The 0o600 chmod mirrors what claude
    # writes (the existing file is mode 600 in observed installations).
    tmp = config_file.with_name(config_file.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        os.chmod(tmp, 0o600)
        os.replace(tmp, config_file)
    except OSError as exc:
        logger.debug("ensure_initialized: write to %s failed: %s", config_file, exc)
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        return False
    return True
