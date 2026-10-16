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
import os
from pathlib import Path

import typer
from rich.console import Console

from claude_task_runner.config.loader import (
    AccountResolution,
    ConfigError,
    config_error_exit_status,
    load_settings,
)
from claude_task_runner.config.schema import Settings
from claude_task_runner.queue.store import require_queue_dir
from claude_task_runner.runner.account_dispatch import QueueCeiling

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
       of the accounts declared in the queue's TOML. Anything at that
       name is returned, not only a file: a directory, a FIFO, a device,
       a symlink whose target is missing (on a mount not up yet, say) or
       that loops. ``load_settings`` then reports it (a missing target as
       one that may come back, exit 1; the others as content errors,
       exit 78) rather than the defaults and their default account
       applying.
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
    if os.path.lexists(candidate):
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


def queue_ceiling_text(ceiling: QueueCeiling, settings: Settings) -> str:
    """The queue's concurrency ceiling on one line, with its warm-up cap when set.

    ``account list`` and ``supervisor status`` print it.
    """
    text = ceiling.describe()
    initial = settings.concurrency.initial_concurrency
    if initial is not None:
        text += f"; initial_concurrency {initial} until a first task completes"
    return text


def queue_ceiling_json(ceiling: QueueCeiling, settings: Settings) -> dict[str, object]:
    """The ``queue_ceiling`` object of ``--json`` output, with the warm-up cap."""
    return {**ceiling.as_json(), "initial_concurrency": settings.concurrency.initial_concurrency}


def broken_accounts_message(
    resolution: AccountResolution,
    *,
    refused: str = "supervisor not started",
    then: str = "start the supervisor again",
    if_transient: str = "so this start exits 1 and systemd tries again; fix it if it does not",
) -> str:
    """Why a command refuses to go on: every account that cannot take tasks.

    ``supervisor start`` uses the defaults; ``install`` passes its own
    ``refused``, ``then`` and ``if_transient``. The line saying each may
    clear up by itself appears when ``resolution.only_transient``, the
    test the caller's exit status follows.
    """
    broken = resolution.broken
    noun = "account" if len(broken) == 1 else "accounts"
    lines = [f"{refused}: {len(broken)} {noun} cannot take tasks:"]
    lines += [f"  {b.name}: {b.error}" for b in broken]
    if resolution.only_transient:
        lines.append(
            "Each may clear up by itself (a config_dir on a mount that is not up yet, "
            f"say), {if_transient}."
        )
    lines.append(
        f"Fix each one (a runner-account.toml can also be deleted for the defaults), then "
        f"{then}. Once the supervisor runs, a problem like this only stops new tasks on its "
        "account."
    )
    return "\n".join(lines)


def load_settings_or_exit(
    config: Path | None,
    console: Console,
    *,
    json: bool = False,
    lead: str = "configuration error",
) -> Settings:
    """``load_settings(config)``, or print why it failed and exit.

    Exits 78 when the configuration's content is wrong and 1 when a file
    could not be read and may come back (:attr:`ConfigError.transient`),
    the rule ``supervisor start`` follows. The error is printed whole
    without a traceback, after ``lead``; with ``json`` it goes to stdout
    as ``{"ok": false, "error": ...}``.
    """
    try:
        return load_settings(config)
    except ConfigError as exc:
        message = f"{lead}: {exc}"
        if json:
            print(_json.dumps({"ok": False, "error": message}))
        else:
            console.print(
                message,
                style="bold red",
                markup=False,
                emoji=False,
                highlight=False,
                soft_wrap=True,
            )
        raise typer.Exit(code=config_error_exit_status(transient=exc.transient)) from exc
