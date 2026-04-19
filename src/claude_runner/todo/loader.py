"""Parse a todo directory of YAML files into validated TaskSpecs."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

from claude_runner.config import Settings
from claude_runner.todo.schema import TaskSpec, build_task, detect_cycles

_log = logging.getLogger(__name__)


@dataclass(slots=True)
class LoadError:
    path: Path
    message: str


@dataclass(slots=True)
class LoadResult:
    tasks: list[TaskSpec]
    errors: list[LoadError]

    def has_errors(self) -> bool:
        return bool(self.errors)


def _yaml_files(todo_dir: Path) -> list[Path]:
    patterns = ("*.yaml", "*.yml")
    found: list[Path] = []
    for pat in patterns:
        found.extend(sorted(todo_dir.glob(pat)))
    return found


def load_task_file(path: Path, *, settings: Settings) -> TaskSpec:
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if raw is None:
        raise ValueError(f"{path}: file is empty")
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top-level YAML must be a mapping")
    return build_task(raw=raw, source_path=path, settings=settings)


def load_todo_dir(todo_dir: Path, *, settings: Settings) -> LoadResult:
    """Parse every YAML in `todo_dir`. Malformed files become LoadErrors."""
    if not todo_dir.is_dir():
        return LoadResult(tasks=[], errors=[LoadError(todo_dir, "todo directory does not exist")])

    tasks: list[TaskSpec] = []
    errors: list[LoadError] = []
    seen_ids: dict[str, Path] = {}

    for path in _yaml_files(todo_dir):
        try:
            spec = load_task_file(path, settings=settings)
        except Exception as exc:
            _log.warning("skipping %s: %s", path, exc)
            errors.append(LoadError(path, str(exc)))
            continue

        if spec.id in seen_ids:
            errors.append(
                LoadError(
                    path,
                    f"duplicate task id {spec.id!r} (also in {seen_ids[spec.id]})",
                )
            )
            continue

        seen_ids[spec.id] = path
        tasks.append(spec)

    cycle = detect_cycles(tasks)
    if cycle:
        errors.append(
            LoadError(
                todo_dir,
                f"dependency cycle detected among ids: {' -> '.join(cycle)}",
            )
        )

    return LoadResult(tasks=tasks, errors=errors)
