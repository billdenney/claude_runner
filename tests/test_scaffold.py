from __future__ import annotations

from pathlib import Path

from claude_runner.config import Settings, load_settings
from claude_runner.scaffold import init_project, next_id, slugify, write_new_task
from claude_runner.todo.loader import load_task_file


def test_slugify_basic() -> None:
    assert slugify("Summarize the README") == "summarize-the-readme"
    assert slugify("  !! hello, world !! ") == "hello-world"
    assert slugify("") == "task"


def test_next_id_gap_aware(tmp_path: Path) -> None:
    (tmp_path / "001-x.yaml").touch()
    (tmp_path / "003-y.yaml").touch()
    assert next_id(tmp_path) == "002"


def test_init_project_creates_expected_files(tmp_path: Path) -> None:
    result = init_project(tmp_path)
    assert result["config"].exists()
    assert result["example_task"].exists()
    assert result["gitignore"].exists()
    content = result["gitignore"].read_text()
    assert ".claude_runner/" in content


def test_new_task_roundtrips_through_loader(tmp_path: Path) -> None:
    settings = Settings()
    path = write_new_task(
        title="Quick check",
        todo_dir=tmp_path / "todo",
        settings=settings,
        prompt="Say hi.",
        working_dir=tmp_path,
    )
    spec = load_task_file(path, settings=settings)
    assert spec.title == "Quick check"
    assert spec.prompt.strip() == "Say hi."
    assert spec.working_dir == tmp_path


def test_init_writes_config_that_is_loadable(tmp_path: Path) -> None:
    init_project(tmp_path)
    settings = load_settings(tmp_path)
    assert settings.plan in {"pro", "max5", "max20", "team", "custom"}
