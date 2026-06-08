"""Tests for :mod:`claude_task_runner.cli._helpers`.

The helper is small; the value is in pinning the resolution order so
future refactors don't silently regress the operator-friendly
auto-discovery of ``<queue>/claude_runner.toml``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_task_runner.cli._helpers import (
    PER_QUEUE_CONFIG_NAME,
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


def test_per_queue_config_must_be_a_file_not_a_directory(tmp_path: Path) -> None:
    """Defensive: if something at ``<queue>/claude_runner.toml`` is a
    directory (unusual but possible if an operator hand-crafts the queue
    layout wrong), don't return it — fall through to None."""
    queue_dir = tmp_path / "q"
    queue_dir.mkdir()
    (queue_dir / PER_QUEUE_CONFIG_NAME).mkdir()  # directory, not a file
    assert resolve_per_queue_config(None, queue_dir) is None


@pytest.mark.parametrize("name", [PER_QUEUE_CONFIG_NAME])
def test_per_queue_name_is_stable(name: str) -> None:
    """The constant name is part of the public CLI contract — operators
    know to put their config at ``<queue>/claude_runner.toml``."""
    assert name == "claude_runner.toml"
