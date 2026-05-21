"""Tests for the new Task.account field (multi-account dispatch pin).

Covers the optional ``account`` field round-trip via YAML and the
back-compat path (omission yields ``None``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from claude_task_runner.queue.schema import Task
from claude_task_runner.queue.store import (
    load_task,
    task_path_for,
    todo_dir,
    write_task_atomic,
)


def _qdir(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    todo_dir(qd)
    return qd


def test_task_account_field_defaults_to_none() -> None:
    """v2 task YAMLs predate this field; default must be None."""
    task = Task.model_validate({"id": "t1", "title": "t", "prompt": "do thing"})
    assert task.account is None


def test_task_account_round_trip_yaml(tmp_path: Path) -> None:
    qd = _qdir(tmp_path)
    task = Task.model_validate({"id": "t1", "title": "t", "prompt": "do thing", "account": "work"})
    write_task_atomic(task, task_path_for(qd, "t1"))
    reloaded = load_task(task_path_for(qd, "t1"))
    assert reloaded.account == "work"


def test_task_account_empty_string_rejected() -> None:
    """Empty string would silently mean ``None`` to operators — reject loudly."""
    with pytest.raises(ValidationError):
        Task.model_validate({"id": "t1", "title": "t", "prompt": "x", "account": ""})


def test_task_account_unset_round_trip(tmp_path: Path) -> None:
    """Omission survives a YAML round-trip as None (not missing)."""
    qd = _qdir(tmp_path)
    task = Task.model_validate({"id": "t1", "title": "t", "prompt": "do thing"})
    write_task_atomic(task, task_path_for(qd, "t1"))
    reloaded = load_task(task_path_for(qd, "t1"))
    assert reloaded.account is None
