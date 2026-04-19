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


def test_init_does_not_overwrite_existing_config(tmp_path: Path) -> None:
    """Re-running `init` on a dir that already has a config file must leave
    the existing config alone."""
    (tmp_path / "claude_runner.toml").write_text(
        '# my custom config\nplan = "pro"\n', encoding="utf-8"
    )
    init_project(tmp_path)
    # Original content preserved.
    text = (tmp_path / "claude_runner.toml").read_text(encoding="utf-8")
    assert "my custom config" in text


def test_init_appends_gitignore_entry_without_trailing_newline(tmp_path: Path) -> None:
    """If .gitignore exists but doesn't end with a newline, scaffolding must
    prepend one before appending its marker."""
    (tmp_path / ".gitignore").write_text("build/", encoding="utf-8")  # no trailing \n
    init_project(tmp_path)
    content = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    # Both the original and the new marker line should be present.
    assert "build/\n" in content
    assert ".claude_runner/" in content


def test_init_does_not_duplicate_existing_gitignore_entry(tmp_path: Path) -> None:
    """If the marker already exists in .gitignore, `init` leaves it alone."""
    (tmp_path / ".gitignore").write_text("# pre-existing\n.claude_runner/\n", encoding="utf-8")
    init_project(tmp_path)
    content = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert content.count(".claude_runner/") == 1


def test_load_settings_without_project_dir_uses_defaults() -> None:
    """load_settings(None) should not crash and should return defaults."""
    from claude_runner.config import load_settings as _load

    settings = _load(None)
    assert settings.plan == "max5"


def test_next_id_respects_width(tmp_path: Path) -> None:
    from claude_runner.scaffold import next_id

    # Empty directory → 001 (or whatever width=3 pads to).
    assert next_id(tmp_path) == "001"
    # If there's a non-matching filename, the regex skips it.
    (tmp_path / "notes.yaml").touch()
    assert next_id(tmp_path) == "001"


def test_write_new_task_with_empty_title_falls_back(tmp_path: Path) -> None:
    """slugify('') yields 'task', so the filename uses the bare id + -task."""
    from claude_runner.config import Settings
    from claude_runner.scaffold import write_new_task

    path = write_new_task(
        title="",
        todo_dir=tmp_path / "todo",
        settings=Settings(),
        prompt="hello",
        working_dir=tmp_path,
    )
    # Filename is <id>-task.yaml because slugify("") -> "task".
    assert path.name.endswith("-task.yaml")
