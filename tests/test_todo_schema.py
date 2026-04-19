from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from claude_runner.config import Settings
from claude_runner.defaults import EFFORT_TABLE, Effort
from claude_runner.todo.loader import load_task_file, load_todo_dir
from claude_runner.todo.schema import detect_cycles


def _write(path: Path, data: dict) -> Path:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_minimal_task_uses_defaults(tmp_path: Path, settings: Settings) -> None:
    path = _write(
        tmp_path / "001-minimal.yaml",
        {"prompt": "Do the thing.", "working_dir": str(tmp_path)},
    )
    spec = load_task_file(path, settings=settings)
    assert spec.id == "001-minimal"
    assert spec.title == "Do the thing."
    assert spec.effort == settings.default_effort
    tier = EFFORT_TABLE[spec.effort]
    assert spec.max_turns == tier.max_turns
    assert spec.estimated_input_tokens == tier.estimated_input_tokens
    assert spec.model == settings.default_model
    assert tuple(spec.allowed_tools) == tuple(settings.default_allowed_tools)


def test_explicit_overrides_beat_defaults(tmp_path: Path, settings: Settings) -> None:
    path = _write(
        tmp_path / "002-override.yaml",
        {
            "prompt": "go",
            "working_dir": str(tmp_path),
            "effort": "high",
            "max_turns": 5,
            "estimated_input_tokens": 42,
        },
    )
    spec = load_task_file(path, settings=settings)
    assert spec.effort == Effort.HIGH
    assert spec.max_turns == 5
    assert spec.estimated_input_tokens == 42


def test_missing_prompt_errors(tmp_path: Path, settings: Settings) -> None:
    path = _write(tmp_path / "003.yaml", {"working_dir": str(tmp_path)})
    with pytest.raises(ValueError, match="prompt"):
        load_task_file(path, settings=settings)


def test_missing_working_dir_errors(tmp_path: Path, settings: Settings) -> None:
    path = _write(tmp_path / "004.yaml", {"prompt": "x"})
    with pytest.raises(ValueError, match="working_dir"):
        load_task_file(path, settings=settings)


def test_duplicate_ids_surface_as_errors(tmp_path: Path, settings: Settings) -> None:
    _write(tmp_path / "a.yaml", {"id": "dup", "prompt": "x", "working_dir": str(tmp_path)})
    _write(tmp_path / "b.yaml", {"id": "dup", "prompt": "y", "working_dir": str(tmp_path)})
    result = load_todo_dir(tmp_path, settings=settings)
    assert result.has_errors()
    assert any("duplicate" in e.message for e in result.errors)


def test_cycle_detection(tmp_path: Path, settings: Settings) -> None:
    _write(
        tmp_path / "a.yaml",
        {"id": "a", "prompt": "x", "working_dir": str(tmp_path), "depends_on": ["b"]},
    )
    _write(
        tmp_path / "b.yaml",
        {"id": "b", "prompt": "y", "working_dir": str(tmp_path), "depends_on": ["a"]},
    )
    result = load_todo_dir(tmp_path, settings=settings)
    cycle = detect_cycles(result.tasks)
    assert cycle  # non-empty
    assert any("cycle" in e.message for e in result.errors)


def test_bad_effort_errors(tmp_path: Path, settings: Settings) -> None:
    path = _write(
        tmp_path / "005.yaml",
        {"prompt": "x", "working_dir": str(tmp_path), "effort": "nonsense"},
    )
    with pytest.raises(ValueError):
        load_task_file(path, settings=settings)
