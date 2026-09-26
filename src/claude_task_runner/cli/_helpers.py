"""Shared helpers for CLI subcommands.

The helpers here are intentionally CLI-coupled — they bridge typer
options (``--queue`` and ``--config``) into the pure config loaders in
:mod:`claude_task_runner.config.loader` and the queue store in
:mod:`claude_task_runner.queue.store`. Keeping them out of
``config/loader.py`` avoids pulling queue-dir-aware path logic into a
module that is otherwise a pure settings + per-account loader.
"""

from __future__ import annotations

import json as _json
from pathlib import Path

import typer
from rich.console import Console

from claude_task_runner.queue.store import require_queue_dir

PER_QUEUE_CONFIG_NAME = "claude_runner.toml"
"""Conventional filename for a per-queue runner config sitting at
``<queue>/claude_runner.toml``."""

CWD_DEFAULT_LABEL = "current directory"
"""What ``--help`` shows as the default of an option whose default is ``Path.cwd``.

Such an option passes the method, not ``Path.cwd()``, so click calls it
when the command runs, and the default is the directory the command runs
in. Typer shows a callable default with ``str()`` unless it is a plain
function, and ``Path.cwd`` is a bound method, so without
``show_default=CWD_DEFAULT_LABEL`` help printed
``[default: <bound method Path.cwd of <class 'pathlib.Path'>>]``. With it,
help prints ``[default: (current directory)]``.
``tests/unit/test_docs_cli_help.py`` gates the help and the default.
"""


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


def require_queue_option(queue_dir: Path, console: Console, *, json: bool = False) -> Path:
    """Resolve ``--queue`` for a command that writes under it, or exit 2.

    See :func:`~claude_task_runner.queue.store.require_queue_dir`: the
    helpers that create the queue's subdirectories would recreate a
    mistyped, deleted or moved ``--queue`` as an empty queue.

    The error is one line on ``console``, printed without Rich markup so
    a ``[`` in the path stays as typed, and without wrapping. With
    ``json`` it goes to stdout as ``{"ok": false, "error": ...}``, the
    shape ``queue force-dispatch --json`` uses for its other errors.
    """
    try:
        return require_queue_dir(queue_dir)
    except NotADirectoryError as exc:
        message = f"--queue is {exc}"
        if json:
            print(_json.dumps({"ok": False, "error": message}))
        else:
            console.print(message, style="bold red", markup=False, highlight=False, soft_wrap=True)
        raise typer.Exit(code=2) from exc
