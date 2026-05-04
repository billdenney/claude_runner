"""Settings loader: package defaults overlaid by per-queue TOML."""

from __future__ import annotations

import tomllib
from importlib import resources
from pathlib import Path
from typing import Any

from claude_task_runner.config.schema import Settings


class ConfigError(ValueError):
    """Raised when settings cannot be loaded or validated."""


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursive dict merge: nested dicts merge, scalars and lists overwrite."""
    out = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_defaults() -> dict[str, Any]:
    """Load the package's default settings TOML."""
    pkg = resources.files("claude_task_runner.config.defaults")
    with (pkg / "settings.toml").open("rb") as fh:
        return tomllib.load(fh)


def load_settings(per_queue_toml: Path | None = None) -> Settings:
    """Load defaults and merge an optional per-queue claude_runner.toml on top.

    Parameters
    ----------
    per_queue_toml
        Path to the per-queue TOML file. If None, only defaults are used.

    Raises
    ------
    ConfigError
        If the per-queue TOML doesn't exist, fails to parse, or the merged
        settings fail schema validation.
    """
    merged = load_defaults()

    if per_queue_toml is not None:
        if not per_queue_toml.exists():
            raise ConfigError(f"Settings file not found: {per_queue_toml}")
        try:
            with per_queue_toml.open("rb") as fh:
                override = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"Invalid TOML in {per_queue_toml}: {exc}") from exc
        merged = _deep_merge(merged, override)

    try:
        return Settings.model_validate(merged)
    except Exception as exc:
        raise ConfigError(f"Settings validation failed: {exc}") from exc
