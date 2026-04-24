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
    plan: Literal["pro", "max5", "max20", "team", "custom", "auto"] = "max5"
    """Budget plan selector.

    * ``pro`` / ``max5`` / ``max20`` / ``team`` — use the static preset from
      ``PLAN_PRESETS`` (see ``defaults.py``).
    * ``custom`` — use ``budget_5h_tokens`` / ``budget_weekly_tokens``
      verbatim; intended for accounts with explicit quotas.
    * ``auto`` — probe the configured ``budget_source`` for the operator's
      own historical usage and calibrate both budgets to the p90 of their
      non-gap 5h blocks and their completed-week totals. Falls back to the
      ``max5`` preset if the source exposes fewer than 10 historical
      blocks or is unavailable. Recommended for Claude Code subscribers
      whose real cache-heavy per-block usage (50-150M tokens) dwarfs the
      static max5 preset (2M tokens).
    """

    budget_5h_tokens: int | None = None
    budget_weekly_tokens: int | None = None

    budget_source: Literal["ccusage", "claude_usage", "context", "api_headers", "static"] = (
        "ccusage"
    )
    backend: Literal["asyncio", "subprocess"] = "asyncio"

    max_concurrency: int = Field(default=8, ge=1)
    """Ceiling on ``target_concurrency`` once the per-task token EMA has
    warmed up. The budget controller will grow target_concurrency toward
    this limit as observed usage allows, but never above it."""

    initial_concurrency: int | None = Field(default=None, ge=1)
    """Starting concurrency used until the per-task token EMA has at
    least ``ema_warm_after`` completions of data to extrapolate from.

    Before the EMA is warm, ``target_concurrency`` returns
    ``min(initial_concurrency, max_concurrency)``; after it's warm, the
    controller resumes its utilization-based sizing capped at
    ``max_concurrency``. Defaults to ``max_concurrency`` (i.e. no
    warm-up ramp — historical behavior) when unset.

    Typical use: set ``initial_concurrency = 1`` or ``2`` at the start
    of a large batch so the first task gives the runner a realistic
    tokens-per-task signal before it spawns N parallel Opus sessions
    and burns through a 5-hour rate limit in 15 minutes."""

    ema_warm_after: int = Field(default=1, ge=1)
    """How many completed tasks are required before the EMA is
    considered reliable enough to scale concurrency off of. While
    fewer than this many tasks have completed, ``target_concurrency``
    is capped at ``initial_concurrency``. Default 1 — after the very
    first completion we have a real signal."""

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

    # Whether to automatically prepend a runner-provided preamble to every
    # task prompt explaining the runtime environment: sidecar stop-and-ask
    # protocol (via CLAUDE_RUNNER_SIDECAR_DIR), pre-set git worktree (when
    # applicable), and gh-read-only workflow. Individual tasks can override
    # via ``inject_preamble: false`` on the task YAML. Default on — the
    # preamble is small and universally helpful.
    inject_preamble: bool = True

    @field_validator("failure_rolling_window")
    @classmethod
    def _rolling_gte_min_samples(cls, v: int, info: Any) -> int:
        min_samples = info.data.get("failure_rate_min_samples", 4)
        if v < min_samples:
            raise ValueError("failure_rolling_window must be >= failure_rate_min_samples")
        return v

    @field_validator("initial_concurrency")
    @classmethod
    def _initial_le_max(cls, v: int | None, info: Any) -> int | None:
        if v is None:
            return None
        max_c = info.data.get("max_concurrency", 8)
        if v > max_c:
            raise ValueError(f"initial_concurrency ({v}) must be <= max_concurrency ({max_c})")
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
