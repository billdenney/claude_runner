"""Every command that takes ``--queue`` must leave a missing queue missing.

The store's directory helpers used to create the queue directory with
``mkdir(parents=True)``, so a read-only command given a mistyped, deleted
or moved ``--queue`` created ``<queue>/todo/`` or
``<queue>/.claude_task_runner/`` and then reported an empty queue, exit 0.
That is "empty because it is broken" reported as "empty because nothing
matched". ``account pause`` went further and wrote ``supervisor.json`` into
the new tree, and ``sidecar answer --allow-partial`` wrote a response file.

This gate walks the CLI, so a command added later is covered as soon as it
takes ``--queue``: :data:`ARGV` must name it, its run against a missing
queue must create nothing, and :data:`OUTCOMES` pins what it prints and
its exit code. The outcome matters as much as the directory: the store no
longer creates a missing queue, so a command that lost its up-front check
would still leave the queue missing, but exit 1 with a traceback. A command
that is not fixed yet goes in :data:`CREATES_MISSING_QUEUE` with the
reason, and a staleness test fails once the entry is no longer needed.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, NamedTuple
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


class Outcome(NamedTuple):
    """How a command run against a missing ``--queue`` ends.

    ``{queue}`` in ``stdout`` and ``stderr`` stands for the missing path,
    resolved. With ``exact`` False, ``stdout`` need only appear in the
    output, for ``doctor``, whose other checks depend on the machine.
    """

    exit_code: int
    stdout: str
    stderr: str = ""
    exact: bool = True


REFUSED = "--queue is not an existing directory: {queue}\n"
"""What :func:`claude_task_runner.cli._helpers.require_queue_option` prints."""

NO_PID_FILE = "No PID file at {queue}/.claude_task_runner/supervisor.pid\n"

OUTCOMES: dict[tuple[str, ...], Outcome] = {
    ("account", "list"): Outcome(2, REFUSED),
    ("account", "pause"): Outcome(2, REFUSED),
    ("account", "resume"): Outcome(2, REFUSED),
    # A diagnostic: the queue_layout check FAILs, the checks that do not
    # read the queue still run, and the ones that do are left out.
    ("doctor",): Outcome(
        1,
        "  FAIL queue_layout: queue dir is not an existing directory: {queue}; "
        "the checks that read the queue did not run\n"
        "      Check --queue (it defaults to the current directory). "
        "To start a new queue there: mkdir -p {queue}/todo\n",
        exact=False,
    ),
    ("install",): Outcome(2, REFUSED),
    ("queue", "add"): Outcome(2, REFUSED),
    ("queue", "backfill-working-dir"): Outcome(2, REFUSED),
    ("queue", "force-dispatch"): Outcome(2, REFUSED),
    ("queue", "list"): Outcome(2, REFUSED),
    ("queue", "restart-fresh"): Outcome(2, REFUSED),
    ("queue", "show"): Outcome(2, REFUSED),
    ("queue", "states"): Outcome(2, REFUSED),
    # The sidecar commands print their errors on stderr.
    ("sidecar", "answer"): Outcome(2, "", REFUSED),
    ("sidecar", "list"): Outcome(2, "", REFUSED),
    ("sidecar", "show"): Outcome(2, "", REFUSED),
    # stop and drain only read the PID file, and never create anything.
    ("supervisor", "drain"): Outcome(1, NO_PID_FILE),
    ("supervisor", "start"): Outcome(2, REFUSED),
    ("supervisor", "status"): Outcome(2, REFUSED),
    ("supervisor", "stop"): Outcome(1, NO_PID_FILE),
    ("watchdog", "register"): Outcome(
        2, "", "register failed: not an existing directory: {queue}\n"
    ),
    # Removes a registry entry and never reads the queue. The directory
    # need not exist, so a queue that was deleted or moved can be dropped.
    ("watchdog", "unregister"): Outcome(0, "not registered: {queue}\n"),
    ("worktree", "reclaim"): Outcome(
        2, "", "error: {queue} is not a queue directory: it has no todo/ subdirectory\n"
    ),
}
"""How each ``--queue`` command ends when the queue is missing."""

JSON_OUTCOMES: dict[tuple[str, ...], tuple[int, dict[str, object]]] = {
    **{
        path: (2, {"ok": False, "error": REFUSED.rstrip()})
        for path in [
            ("account", "list"),
            ("account", "pause"),
            ("account", "resume"),
            ("queue", "backfill-working-dir"),
            ("queue", "force-dispatch"),
            ("queue", "list"),
            ("queue", "restart-fresh"),
            ("queue", "show"),
            ("queue", "states"),
            ("sidecar", "list"),
            ("sidecar", "show"),
            ("supervisor", "status"),
        ]
    },
    ("worktree", "reclaim"): (
        2,
        {"ok": False, "error": "{queue} is not a queue directory: it has no todo/ subdirectory"},
    ),
}
"""The JSON on stdout of each ``--queue`` command with ``--json``, but
``doctor``, whose JSON keeps its usual shape; see
:meth:`TestMissingQueue.test_doctor_json_keeps_its_shape`."""

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


def _takes(command: Any, option: str) -> bool:
    return any(option in param.opts for param in command.params)


def _queue_paths() -> list[tuple[str, ...]]:
    """Every command path whose command takes ``--queue``."""
    return [path for path in _command_paths() if path and _takes(_node(path), "--queue")]


def _argv(path: tuple[str, ...], queue: Path, config: Path, *extra: str) -> list[str]:
    rest = [config.as_posix() if arg == CONFIG else arg for arg in ARGV[path]]
    return [*path, *rest, *extra, "--queue", queue.as_posix()]


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


_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
"""An ANSI escape sequence, in case the environment forces colour."""


def _run(path: tuple[str, ...], queue: Path, tmp_path: Path, *extra: str) -> Any:
    patches = [patch(target, _unreachable) for target in _SIDE_EFFECTS]
    for p in patches:
        p.start()
    try:
        # COLUMNS keeps Rich from wrapping a long path across lines.
        return CliRunner().invoke(
            app, _argv(path, queue, _config(tmp_path), *extra), env={"COLUMNS": "1000"}
        )
    finally:
        for p in patches:
            p.stop()


def _plain(text: str) -> str:
    return _ANSI.sub("", text)


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

    def test_outcomes_name_every_queue_command(self) -> None:
        assert set(OUTCOMES) == set(ARGV)

    def test_json_outcomes_name_every_json_queue_command(self) -> None:
        with_json = {path for path in ARGV if _takes(_node(path), "--json")}
        assert set(JSON_OUTCOMES) == with_json - {("doctor",)}


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

    @pytest.mark.parametrize("path", sorted(OUTCOMES), ids=_path_id)
    def test_outcome(self, path: tuple[str, ...], tmp_path: Path) -> None:
        queue = tmp_path / "gone" / "queue"
        result = _run(path, queue, tmp_path)
        expected = OUTCOMES[path]
        stdout = expected.stdout.format(queue=queue.resolve())
        stderr = expected.stderr.format(queue=queue.resolve())
        assert result.exit_code == expected.exit_code, result.output
        if expected.exact:
            assert _plain(result.stdout) == stdout
        else:
            assert stdout in _plain(result.stdout)
        assert _plain(result.stderr) == stderr

    @pytest.mark.parametrize("path", sorted(JSON_OUTCOMES), ids=_path_id)
    def test_json_outcome(self, path: tuple[str, ...], tmp_path: Path) -> None:
        queue = tmp_path / "gone" / "queue"
        result = _run(path, queue, tmp_path, "--json")
        exit_code, payload = JSON_OUTCOMES[path]
        error = str(payload["error"]).format(queue=queue.resolve())
        assert result.exit_code == exit_code, result.output
        assert json.loads(result.stdout) == {**payload, "error": error}
        assert result.stderr == ""
        assert not queue.parent.exists()

    def test_doctor_json_keeps_its_shape(self, tmp_path: Path) -> None:
        queue = tmp_path / "gone" / "queue"
        result = _run(("doctor",), queue, tmp_path, "--json")
        assert result.exit_code == 1, result.output
        payload = json.loads(result.stdout)
        assert payload["queue_dir"] == str(queue.resolve())
        assert [r["name"] for r in payload["results"]] == [
            "claude_binary",
            "accounts",
            "legacy_claude_config_dir",
            "account_policies",
            "dispatch_pct_legacy",
            "account_sudo",
            "queue_perms_multi_user",
            "global_lock",
            "queue_layout",
            "skills_installed",
            "watchdog_installed",
        ]
        assert payload["results"][8] == {
            "name": "queue_layout",
            "status": "fail",
            "detail": f"queue dir is not an existing directory: {queue.resolve()}; "
            "the checks that read the queue did not run",
            "remediation": "Check --queue (it defaults to the current directory). "
            f"To start a new queue there: mkdir -p {queue.resolve()}/todo",
        }
        assert not queue.parent.exists()

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
