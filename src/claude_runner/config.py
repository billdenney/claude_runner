"""Pydantic-settings model for claude_runner.toml / env config."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from claude_runner.defaults import (
    DEFAULT_ALLOWED_TOOLS,
    DEFAULT_DISCOVERY_CACHE_TTL_S,
    DEFAULT_MODEL,
    PLAN_PRESETS,
    Effort,
)

CONFIG_FILENAME = "claude_runner.toml"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CLAUDE_RUNNER_", extra="ignore")

    regime: Literal["pro_max", "api"] = "pro_max"
    plan: Literal["pro", "max5", "max20", "team", "custom"] = "max5"

    budget_5h_tokens: int | None = None
    budget_weekly_tokens: int | None = None

    budget_source: Literal["ccusage", "context", "api_headers", "static"] = "ccusage"
    backend: Literal["asyncio", "subprocess"] = "asyncio"

    max_concurrency: int = Field(default=8, ge=1)
    min_utilization: float = Field(default=0.80, ge=0.0, le=1.0)
    weekly_guard: float = Field(default=0.90, ge=0.0, le=1.0)
    refresh_interval_s: int = Field(default=30, ge=1)

    max_consecutive_failures: int = Field(default=3, ge=1)
    failure_rate_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    failure_rolling_window: int = Field(default=10, ge=1)
    failure_rate_min_samples: int = Field(default=4, ge=1)

    default_effort: Effort = Effort.MEDIUM
    default_model: str = DEFAULT_MODEL
    default_allowed_tools: list[str] = Field(default_factory=lambda: list(DEFAULT_ALLOWED_TOOLS))

    discovery_cache_ttl_s: int = Field(default=DEFAULT_DISCOVERY_CACHE_TTL_S, ge=1)

    todo_subdir: str = "todo"
    state_subdir: str = ".claude_runner"

    # Sidecar reporting loop (see src/claude_runner/sidecar/).
    reporting_interval_s: int = Field(default=60, ge=1)
    """Cadence for awaiting_input_snapshot events and status_snapshot.json writes."""
    report_max_per_tick: int = Field(default=5, ge=1)
    """Max awaiting-input tasks emitted per reporting tick (the snapshot file always lists all)."""

    # Optional default parent directory for per-task git worktrees. When set,
    # tasks whose YAML has a ``git_worktree`` block will be checked out into
    # ``<worktree_root>/<task_id>`` unless the YAML overrides the location. The
    # literal token ``${task_id}`` is substituted at dispatch time; if absent
    # it is appended as a subdirectory.
    worktree_root: str | None = None

    @field_validator("failure_rolling_window")
    @classmethod
    def _rolling_gte_min_samples(cls, v: int, info: Any) -> int:
        min_samples = info.data.get("failure_rate_min_samples", 4)
        if v < min_samples:
            raise ValueError("failure_rolling_window must be >= failure_rate_min_samples")
        return v

    def resolved_budget_5h(self) -> int:
        if self.budget_5h_tokens is not None:
            return self.budget_5h_tokens
        return PLAN_PRESETS[self.plan].budget_5h_tokens

    def resolved_budget_weekly(self) -> int:
        if self.budget_weekly_tokens is not None:
            return self.budget_weekly_tokens
        return PLAN_PRESETS[self.plan].budget_weekly_tokens


def load_settings(project_dir: Path | None = None) -> Settings:
    """Load settings: TOML file under `project_dir` if present; env-vars win."""
    import os

    file_values: dict[str, Any] = {}
    if project_dir is not None:
        config_path = project_dir / CONFIG_FILENAME
        if config_path.is_file():
            with config_path.open("rb") as fh:
                file_values = tomllib.load(fh)

    # Drop any file-provided value whose env-var equivalent is set, so env wins.
    env_prefix = Settings.model_config.get("env_prefix", "")
    filtered = {
        k: v for k, v in file_values.items() if f"{env_prefix}{k.upper()}" not in os.environ
    }
    return Settings(**filtered)
