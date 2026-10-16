"""Tests for :mod:`claude_task_runner.cli._helpers`.

The helper is small; the value is in pinning the resolution order so
future refactors don't silently regress the operator-friendly
auto-discovery of ``<queue>/claude_runner.toml``.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest
import typer
from rich.console import Console

from claude_task_runner.cli._helpers import (
    PER_QUEUE_CONFIG_NAME,
    require_queue_option,
    resolve_per_queue_config,
)


def test_explicit_config_takes_precedence(tmp_path: Path) -> None:
    """When --config is non-None, return it unchanged regardless of whether
    a per-queue file exists alongside it."""
    explicit = tmp_path / "elsewhere.toml"
    explicit.write_text("")
    queue_dir = tmp_path / "q"
    queue_dir.mkdir()
    # Even with a per-queue TOML present, explicit wins.
    (queue_dir / PER_QUEUE_CONFIG_NAME).write_text("")
    assert resolve_per_queue_config(explicit, queue_dir) == explicit


def test_explicit_config_is_returned_even_if_nonexistent(tmp_path: Path) -> None:
    """Don't second-guess the operator: a missing explicit --config path
    is returned verbatim so ``load_settings`` raises a helpful error."""
    explicit = tmp_path / "does_not_exist.toml"
    queue_dir = tmp_path / "q"
    queue_dir.mkdir()
    assert resolve_per_queue_config(explicit, queue_dir) == explicit


def test_auto_discovers_per_queue_config(tmp_path: Path) -> None:
    """When --config is None and ``<queue>/claude_runner.toml`` exists,
    return that path."""
    queue_dir = tmp_path / "q"
    queue_dir.mkdir()
    per_queue = queue_dir / PER_QUEUE_CONFIG_NAME
    per_queue.write_text("")
    assert resolve_per_queue_config(None, queue_dir) == per_queue


def test_falls_back_to_none_when_neither_present(tmp_path: Path) -> None:
    """When --config is None AND no ``<queue>/claude_runner.toml`` exists,
    return None — ``load_settings(None)`` then uses package defaults
    (matches historical no-config behaviour)."""
    queue_dir = tmp_path / "q"
    queue_dir.mkdir()
    assert resolve_per_queue_config(None, queue_dir) is None


@pytest.mark.parametrize("linked", [False, True], ids=["directory", "symlink-to-directory"])
def test_a_directory_at_the_per_queue_name_is_returned(tmp_path: Path, linked: bool) -> None:
    """Not the defaults, whose default account is ``~/.claude``:
    ``load_settings`` then refuses it as a content error."""
    queue_dir = tmp_path / "q"
    queue_dir.mkdir()
    per_queue = queue_dir / PER_QUEUE_CONFIG_NAME
    if linked:
        (tmp_path / "elsewhere").mkdir()
        per_queue.symlink_to(tmp_path / "elsewhere")
    else:
        per_queue.mkdir()
    assert resolve_per_queue_config(None, queue_dir) == per_queue


@pytest.mark.parametrize("target", ["missing", "loop"])
def test_a_symlink_to_no_file_is_returned(tmp_path: Path, target: str) -> None:
    """Not the defaults, which name another account: ``load_settings`` then
    reports the symlink."""
    queue_dir = tmp_path / "q"
    queue_dir.mkdir()
    per_queue = queue_dir / PER_QUEUE_CONFIG_NAME
    per_queue.symlink_to(
        tmp_path / "unmounted" / "claude_runner.toml" if target == "missing" else per_queue
    )
    assert resolve_per_queue_config(None, queue_dir) == per_queue


@pytest.mark.parametrize("kind", ["fifo", "dev-null"])
def test_anything_else_at_the_per_queue_name_is_returned(tmp_path: Path, kind: str) -> None:
    """``load_settings`` refuses what is not a regular file; the lookup itself
    does not open it, so a FIFO cannot block it."""
    queue_dir = tmp_path / "q"
    queue_dir.mkdir()
    per_queue = queue_dir / PER_QUEUE_CONFIG_NAME
    if kind == "fifo":
        os.mkfifo(per_queue)
    else:
        per_queue.symlink_to("/dev/null")
    assert resolve_per_queue_config(None, queue_dir) == per_queue


def test_a_symlink_to_a_file_is_returned(tmp_path: Path) -> None:
    queue_dir = tmp_path / "q"
    queue_dir.mkdir()
    real = tmp_path / "real.toml"
    real.write_text("")
    (queue_dir / PER_QUEUE_CONFIG_NAME).symlink_to(real)
    assert resolve_per_queue_config(None, queue_dir) == queue_dir / PER_QUEUE_CONFIG_NAME


@pytest.mark.parametrize("name", [PER_QUEUE_CONFIG_NAME])
def test_per_queue_name_is_stable(name: str) -> None:
    """The constant name is part of the public CLI contract — operators
    know to put their config at ``<queue>/claude_runner.toml``."""
    assert name == "claude_runner.toml"


def _console() -> tuple[Console, io.StringIO]:
    out = io.StringIO()
    return Console(file=out, width=40), out


class TestRequireQueueOption:
    """``--queue`` for a command that writes under it must be an existing directory."""

    def test_existing_directory_is_returned_resolved(self, tmp_path: Path) -> None:
        queue = tmp_path / "q"
        queue.mkdir()
        console, out = _console()
        assert require_queue_option(queue, console) == queue.resolve()
        assert out.getvalue() == ""

    def test_missing_directory_exits_2_with_one_unwrapped_line(self, tmp_path: Path) -> None:
        """The console is 40 columns wide; the line must not wrap anyway."""
        missing = tmp_path / "a-queue-directory-that-does-not-exist"
        console, out = _console()
        with pytest.raises(typer.Exit) as excinfo:
            require_queue_option(missing, console)
        assert excinfo.value.exit_code == 2
        assert out.getvalue() == f"--queue is not an existing directory: {missing.resolve()}\n"
        assert not missing.exists()

    def test_path_with_brackets_is_printed_verbatim(self, tmp_path: Path) -> None:
        """Rich would read ``[bold]`` in a path as markup."""
        missing = tmp_path / "[bold]q"
        console, out = _console()
        with pytest.raises(typer.Exit):
            require_queue_option(missing, console)
        assert out.getvalue() == f"--queue is not an existing directory: {missing.resolve()}\n"

    def test_json_mode_prints_the_error_payload(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        missing = tmp_path / "missing"
        console, out = _console()
        with pytest.raises(typer.Exit) as excinfo:
            require_queue_option(missing, console, json=True)
        assert excinfo.value.exit_code == 2
        assert json.loads(capsys.readouterr().out) == {
            "ok": False,
            "error": f"--queue is not an existing directory: {missing.resolve()}",
        }
        assert out.getvalue() == ""
