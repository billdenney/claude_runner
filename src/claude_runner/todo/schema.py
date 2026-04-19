"""Pydantic model for a task YAML file."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from claude_runner.config import Settings
from claude_runner.defaults import EFFORT_TABLE, Effort

Priority = Literal["low", "normal", "high"]


class TaskSpec(BaseModel):
    """User-authored task definition, after default resolution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt: str = Field(min_length=1)
    working_dir: Path

    # Optional fields; defaults are filled during loading.
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    allowed_tools: tuple[str, ...]
    model: str
    effort: Effort
    max_turns: int = Field(ge=1)
    estimated_input_tokens: int = Field(ge=0)
    depends_on: tuple[str, ...] = ()
    priority: Priority = "normal"

    @field_validator("working_dir")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return Path(v).expanduser()

    def priority_rank(self) -> int:
        return {"high": 0, "normal": 1, "low": 2}[self.priority]


def _derive_title(prompt: str) -> str:
    for line in prompt.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:80]
    return "Untitled task"


def build_task(
    *,
    raw: dict[str, object],
    source_path: Path,
    settings: Settings,
) -> TaskSpec:
    """Fill defaults from settings/effort and return a validated TaskSpec."""
    prompt_value = raw.get("prompt")
    if not isinstance(prompt_value, str) or not prompt_value.strip():
        raise ValueError(f"{source_path}: 'prompt' is required and must be a non-empty string")
    if "working_dir" not in raw:
        raise ValueError(f"{source_path}: 'working_dir' is required")

    data: dict[str, object] = dict(raw)

    data.setdefault("id", source_path.stem)
    # YAML parses bare digits as int — coerce to string so the Pydantic model accepts it.
    data["id"] = str(data["id"])
    data.setdefault("title", _derive_title(str(data["prompt"])))
    data["title"] = str(data["title"])

    effort_raw = data.get("effort", settings.default_effort)
    effort = Effort(effort_raw) if not isinstance(effort_raw, Effort) else effort_raw
    data["effort"] = effort
    tier = EFFORT_TABLE[effort]

    data.setdefault("model", settings.default_model)
    if "allowed_tools" not in data:
        data["allowed_tools"] = tuple(settings.default_allowed_tools)
    else:
        raw_tools = data["allowed_tools"]
        if not isinstance(raw_tools, list | tuple):
            raise ValueError(f"{source_path}: 'allowed_tools' must be a list")
        data["allowed_tools"] = tuple(raw_tools)

    if data.get("max_turns") is None:
        data["max_turns"] = tier.max_turns
    if data.get("estimated_input_tokens") is None:
        data["estimated_input_tokens"] = tier.estimated_input_tokens

    if "depends_on" in data and data["depends_on"] is not None:
        deps = data["depends_on"]
        if not isinstance(deps, list | tuple):
            raise ValueError(f"{source_path}: 'depends_on' must be a list")
        data["depends_on"] = tuple(deps)
    else:
        data["depends_on"] = ()

    return TaskSpec.model_validate(data)


def detect_cycles(tasks: list[TaskSpec]) -> list[str]:
    """Return task ids involved in a dependency cycle (empty if acyclic)."""
    graph: dict[str, tuple[str, ...]] = {t.id: t.depends_on for t in tasks}
    white, gray, black = 0, 1, 2
    color: dict[str, int] = dict.fromkeys(graph, white)
    cycle: list[str] = []

    def visit(node: str, stack: list[str]) -> bool:
        color[node] = gray
        stack.append(node)
        for dep in graph.get(node, ()):
            if dep not in graph:
                continue
            if color[dep] == gray:
                idx = stack.index(dep)
                cycle.extend(stack[idx:])
                return True
            if color[dep] == white and visit(dep, stack):
                return True
        stack.pop()
        color[node] = black
        return False

    for n in graph:
        if color[n] == white and visit(n, []):
            break
    return cycle
