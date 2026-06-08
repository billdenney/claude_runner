"""Shared helpers for CLI subcommands.

The helpers here are intentionally CLI-coupled — they bridge typer
options (``--queue`` and ``--config``) into the pure config loaders in
:mod:`claude_task_runner.config.loader`. Keeping them out of
``config/loader.py`` avoids pulling queue-dir-aware path logic into a
module that is otherwise a pure settings + per-account loader.
"""

from __future__ import annotations

from pathlib import Path

PER_QUEUE_CONFIG_NAME = "claude_runner.toml"
"""Conventional filename for a per-queue runner config sitting at
``<queue>/claude_runner.toml``."""


def resolve_per_queue_config(config: Path | None, queue_dir: Path) -> Path | None:
    """Pick the per-queue ``claude_runner.toml`` to feed to ``load_settings``.

    Resolution order:

    1. If the operator passed ``--config`` explicitly, honour it
       verbatim (don't second-guess; absent file there is loud and
       lets ``load_settings`` raise a helpful error).
    2. Otherwise, look for ``<queue>/claude_runner.toml``. If it
       exists, return it. This is the auto-discovery that fixes the
       common pitfall of running ``claude-task-runner account list
       --queue <dir>`` and silently getting package defaults instead
       of the accounts declared in the queue's TOML.
    3. Otherwise, return ``None`` — caller hands that to
       ``load_settings`` which falls back to package defaults
       (matches the historical no-config behaviour).

    Parameters
    ----------
    config
        The value of the ``--config`` / ``-c`` option (``None`` when
        not provided).
    queue_dir
        The value of the ``--queue`` option (defaults to ``cwd`` per
        the per-command typer option; pass the resolved
        ``queue_dir.resolve()`` for stability across symlinks).

    Returns
    -------
    Path | None
        Path to the chosen TOML, or ``None`` when neither an explicit
        ``--config`` nor a per-queue TOML is available.
    """
    if config is not None:
        return config
    candidate = queue_dir / PER_QUEUE_CONFIG_NAME
    if candidate.is_file():
        return candidate
    return None
