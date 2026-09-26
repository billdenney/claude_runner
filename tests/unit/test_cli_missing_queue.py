"""Every command that takes ``--queue`` must leave a missing queue missing.

The store's directory helpers used to create the queue directory with
``mkdir(parents=True)``, so a read-only command given a mistyped, deleted
or moved ``--queue`` created ``<queue>/todo/`` or
``<queue>/.claude_task_runner/`` and then reported an empty queue, exit 0.
That is "empty because it is broken" reported as "empty because nothing
matched". ``account pause`` went further and wrote ``supervisor.json`` into
the new tree, and ``sidecar answer --allow-partial`` wrote a response file.

This gate walks the CLI, so a command added later is covered as soon as it
takes ``--queue``: :data:`ARGV` must name it, and its run against a missing
queue must create nothing. A command that is not fixed yet goes in
:data:`CREATES_MISSING_QUEUE` with the reason, and a staleness test fails
once the entry is no longer needed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from claude_task_runner.cli import app

CLI: Any = typer.main.get_command(app)
"""The click command tree, typed ``Any`` for the reason
``tests/unit/test_docs_cli_refs.py`` gives."""

CONFIG = "{config}"
"""Stands for a per-queue TOML the test writes. ``queue
backfill-working-dir`` exits before it reads the queue unless
``[queue].working_dir_template`` is set."""

ARGV: dict[tuple[str, ...], tuple[str, ...]] = {
    ("account", "list"): (),
    ("account", "pause"): ("default",),
    ("account", "resume"): ("default",),
    ("doctor",): ("--no-check-paths",),
    ("install",): ("--yes",),
    ("queue", "add"): ("--id", "t1", "--title", "t", "--prompt", "p"),
    ("queue", "backfill-working-dir"): ("--config", CONFIG),
    ("queue", "force-dispatch"): ("t1", "--wait-seconds", "0"),
    ("queue", "list"): (),
    ("queue", "restart-fresh"): ("t1",),
    ("queue", "show"): ("t1",),
    ("queue", "states"): (),
    ("sidecar", "answer"): (
        "t1",
        "1",
        "--answers",
        '[{"id": "q1", "value": "A"}]',
        "--allow-partial",
    ),
    ("sidecar", "list"): (),
    ("sidecar", "show"): ("t1", "1"),
    ("supervisor", "drain"): ("--no-wait",),
    ("supervisor", "start"): ("--max-ticks", "1"),
    ("supervisor", "status"): (),
    ("supervisor", "stop"): (),
    ("watchdog", "register"): (),
    ("watchdog", "unregister"): (),
    ("worktree", "reclaim"): (),
}
"""What each ``--queue`` command gets besides ``--queue``.

Enough to get past the command's own argument checks, so that only the
queue can stop it: the most damaging form where there is a choice, such
as ``--allow-partial`` for ``sidecar answer``, which then writes."""

MAY_SUCCEED: dict[tuple[str, ...], str] = {
    ("watchdog", "unregister"): (
        "Removes a registry entry and never reads the queue. The directory "
        "need not exist, so a queue that was deleted or moved can be dropped."
    ),
}
"""Commands that may exit 0 on a missing ``--queue``, each with why."""

CREATES_MISSING_QUEUE: dict[tuple[str, ...], str] = {}
"""Commands not fixed yet, each with why. A missing ``--queue`` is created
by these; every other command must leave it missing."""


def _path_id(path: tuple[str, ...]) -> str:
    return " ".join(path)


def _command_paths(node: Any = CLI, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """Every command path in the tree, groups included."""
    paths = [prefix]
    for name, sub in sorted(getattr(node, "commands", {}).items()):
        paths.extend(_command_paths(sub, (*prefix, name)))
    return paths


def _node(path: tuple[str, ...]) -> Any:
    node = CLI
    for name in path:
        node = node.commands[name]
    return node


def _takes_queue(command: Any) -> bool:
    return any("--queue" in param.opts for param in command.params)


def _queue_paths() -> list[tuple[str, ...]]:
    """Every command path whose command takes ``--queue``."""
    return [path for path in _command_paths() if path and _takes_queue(_node(path))]


def _argv(path: tuple[str, ...], queue: Path, config: Path) -> list[str]:
    rest = [config.as_posix() if arg == CONFIG else arg for arg in ARGV[path]]
    return [*path, *rest, "--queue", queue.as_posix()]


def _config(tmp_path: Path) -> Path:
    config = tmp_path / "claude_runner.toml"
    config.write_text('[queue]\nworking_dir_template = "/tmp/worktrees/{task_id}"\n')
    return config


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep ``watchdog`` and ``install`` out of the developer's real home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def _unreachable(*_args: object, **_kwargs: object) -> object:
    raise AssertionError("a missing --queue reached a side effect")


_SIDE_EFFECTS = (
    "claude_task_runner.cli.supervisor_cmd.start_daemon",
    "claude_task_runner.cli.queue_cmd.fd_mod.dispatch_synchronously",
    "claude_task_runner.cli.install_cmd.systemd_mod.is_systemd_user_available",
    "claude_task_runner.cli.install_cmd.systemd_mod.apply_plan",
    "claude_task_runner.cli.install_cmd.cron_install.build_install_plan",
    "claude_task_runner.cli.install_cmd.cron_install.backup_crontab",
    "claude_task_runner.cli.install_cmd.cron_install.apply_plan",
)
"""What a command could reach if it stopped refusing a missing queue: a
supervisor loop, a ``claude`` run, a crontab or systemd change."""


def _run(path: tuple[str, ...], queue: Path, tmp_path: Path) -> Any:
    patches = [patch(target, _unreachable) for target in _SIDE_EFFECTS]
    for p in patches:
        p.start()
    try:
        return CliRunner().invoke(app, _argv(path, queue, _config(tmp_path)))
    finally:
        for p in patches:
            p.stop()


class TestInventory:
    def test_argv_names_every_queue_command(self) -> None:
        # Equality, not a subset: a new command must be added here, and a
        # removed one dropped.
        assert set(_queue_paths()) == set(ARGV)

    def test_finds_the_queue_commands(self) -> None:
        # Guards the equality above against a walk that finds nothing.
        assert {("queue", "list"), ("doctor",), ("install",)} <= set(_queue_paths())

    @pytest.mark.parametrize("path", sorted(ARGV), ids=_path_id)
    def test_argv_parses(self, path: tuple[str, ...], tmp_path: Path) -> None:
        # Arguments click rejects would stop the command before it reads
        # the queue, and the gate below would pass without testing anything.
        command = _node(path)
        argv = _argv(path, tmp_path / "q", _config(tmp_path))[len(path) :]
        command.make_context(command.name, argv)

    @pytest.mark.parametrize("path", sorted(MAY_SUCCEED), ids=_path_id)
    def test_may_succeed_entries_take_queue(self, path: tuple[str, ...]) -> None:
        assert path in ARGV


class TestMissingQueue:
    @pytest.mark.parametrize("path", sorted(set(ARGV) - set(CREATES_MISSING_QUEUE)), ids=_path_id)
    def test_is_not_created(self, path: tuple[str, ...], tmp_path: Path) -> None:
        # Two levels, so that creating the parent shows up too.
        gone = tmp_path / "gone"
        result = _run(path, gone / "queue", tmp_path)
        assert not gone.exists(), (
            f"`{_path_id(path)} --queue <missing>` created {sorted(gone.rglob('*'))}; "
            f"exit {result.exit_code}, output: {result.output!r}"
        )
        if path not in MAY_SUCCEED:
            assert result.exit_code != 0, result.output

    def test_allowlist_entries_are_still_needed(self, tmp_path: Path) -> None:
        # A stale entry would let the command start creating the queue
        # again unnoticed. One test over all entries, not one per entry, so
        # an empty allowlist is not reported as a skipped test.
        stale = []
        for i, path in enumerate(sorted(CREATES_MISSING_QUEUE)):
            gone = tmp_path / f"gone-{i}"
            _run(path, gone / "queue", tmp_path)
            if not gone.exists():
                stale.append(path)
        assert stale == [], (
            f"CREATES_MISSING_QUEUE is stale for {stale}: these commands no "
            "longer create a missing queue; delete their entries."
        )
