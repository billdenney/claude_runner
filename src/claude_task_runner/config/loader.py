"""Settings loader: package defaults overlaid by per-queue TOML.

Two-layer load:

* :func:`load_settings` returns the queue-side :class:`Settings` (the
  classic defaults + ``claude_runner.toml`` merge).
* :func:`resolve_accounts` walks ``settings.accounts`` and reads each
  account's own ``<config_dir>/runner-account.toml`` for the per-
  account dispatch policy. Composes a :class:`ResolvedAccount` per
  account. Missing per-account file → defaults.

The split keeps the queue config slim (it only declares *which*
accounts to use) while each account owner controls their own
``max_concurrency`` and throttle bands inside their own Claude
config dir.
"""

from __future__ import annotations

import tomllib
from importlib import resources
from pathlib import Path
from typing import Any

from claude_task_runner.config.schema import (
    AccountPolicy,
    ResolvedAccount,
    Settings,
)


class ConfigError(ValueError):
    """Raised when settings cannot be loaded or validated."""


PER_ACCOUNT_TOML_NAME = "runner-account.toml"
"""Filename inside each account's ``CLAUDE_CONFIG_DIR`` that carries
the per-account dispatch policy."""


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


def per_account_toml_path(config_dir: str) -> Path | None:
    """Resolve ``<config_dir>/runner-account.toml``.

    Returns ``None`` when ``config_dir`` is empty (the synthesised
    legacy ``"default"`` account before the operator declares a
    non-empty config_dir). When set, returns the absolute path
    whether or not the file exists.
    """
    if not config_dir:
        return None
    return Path(config_dir).expanduser() / PER_ACCOUNT_TOML_NAME


def load_account_policy(config_dir: str) -> AccountPolicy:
    """Read ``<config_dir>/runner-account.toml`` and return the policy.

    Missing file → all defaults (``max_concurrency=1`` and the
    documented band defaults). Present but unparseable → ConfigError.
    Empty config_dir → defaults (used for the synthesised legacy
    ``"default"`` account before the operator declares an explicit
    config_dir).
    """
    path = per_account_toml_path(config_dir)
    if path is None or not path.exists():
        return AccountPolicy()
    try:
        with path.open("rb") as fh:
            payload = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc
    try:
        return AccountPolicy.model_validate(payload)
    except Exception as exc:
        raise ConfigError(f"Per-account policy validation failed for {path}: {exc}") from exc


def resolve_accounts(settings: Settings) -> list[ResolvedAccount]:
    """Compose each account's queue-side declaration with its per-account policy.

    Walks ``settings.accounts`` in declaration order; for each entry,
    reads ``<config_dir>/runner-account.toml`` via
    :func:`load_account_policy` and produces a
    :class:`ResolvedAccount`. The returned list preserves order so
    callers that tie-break alphabetically can do so explicitly.

    Raises :class:`ConfigError` if any per-account file is unparseable
    or invalid. A missing file is *not* an error — the defaults apply.
    """
    resolved: list[ResolvedAccount] = []
    for acct in settings.accounts:
        policy = load_account_policy(acct.config_dir)
        resolved.append(
            ResolvedAccount(
                name=acct.name,
                config_dir=acct.config_dir,
                linux_user=acct.linux_user,
                policy=policy,
            )
        )
    return resolved
