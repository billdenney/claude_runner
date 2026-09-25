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

from pydantic import ValidationError

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


_LEGACY_THROTTLE_MIGRATION_MSG = (
    "[throttle.*] is gone in ADR-0022; rename to [dispatch_pct.*]. "
    "Mapping: day = {fivehr_slowdown_pct, fivehr_stop_pct}, "
    "night = {fivehr_slowdown_pct, fivehr_stop_pct, time_start, time_end}, "
    "week = {early_pct, eow_pct, eow_time_switch}. "
    "See docs/cheatsheet.md#migration-from-throttle and "
    "docs/decisions/0022-dispatch-pct-trace-following.md."
)


def _reject_legacy_throttle(payload: dict[str, Any], source: str) -> None:
    """Raise :class:`ConfigError` if ``payload`` contains a top-level ``throttle`` key.

    Called on every operator-provided TOML (queue and per-account)
    *before* merging defaults so an operator who hasn't migrated their
    config sees the rename hint at startup — silent fall-through would
    drop safety-floor settings without warning.
    """
    if "throttle" in payload:
        raise ConfigError(f"{source}: {_LEGACY_THROTTLE_MIGRATION_MSG}")


_RETIRED_KEYS: dict[tuple[str, ...], str] = {
    ("claude", "plan"): (
        "selected a [plans.*] token budget, and nothing read those; the throttle "
        "compares the utilization percentages /usage reports against "
        "[dispatch_pct.*] (ADR-0022)"
    ),
    ("plans",): "token budgets nothing read; see [claude].plan",
    ("usage", "healthcheck_interval_s"): "scheduled a drift canary that was never built",
    ("usage", "suspicious_delta_pct"): (
        "tuned a utilization monotonicity check that nothing ever called"
    ),
    ("session", "resume_fail_fast_s"): (
        "timed a fast fall-through from a failed --resume that was never built; "
        "a resume is retried until [session].max_resume_attempts, then goes fresh "
        "(ADR-0005)"
    ),
}
"""Queue-TOML keys removed from the schema because no code ever read them.

Maps each key's path to why it went. A one-segment path is a whole
table. Every key here was inert while it existed, so deleting it from a
TOML changes nothing, which is the one thing the operator needs to hear.
"""


def _retired_key_shown(path: tuple[str, ...]) -> str:
    """``("claude", "plan")`` as an operator writes it: ``[claude].plan``.

    A whole table shows as ``[plans.*]``, covering ``[plans]`` and every
    ``[plans.<name>]`` sub-table.
    """
    if len(path) == 1:
        return f"[{path[0]}.*]"
    return f"[{'.'.join(path[:-1])}].{path[-1]}"


def _has_path(payload: dict[str, Any], path: tuple[str, ...]) -> bool:
    """Whether ``payload`` sets ``path``. A scalar where a table belongs
    stops the walk: that is a type error for schema validation to report."""
    node: Any = payload
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return False
        node = node[key]
    return True


def _reject_retired_keys(payload: dict[str, Any], source: str) -> None:
    """Raise :class:`ConfigError` naming every retired key ``payload`` sets.

    ``extra="forbid"`` would reject these keys anyway, but one at a time
    and without saying that deleting them is safe. This names every
    retired key in the file at once, with the reason it went. Called on
    the queue TOML before defaults are merged, like
    :func:`_reject_legacy_throttle`. The per-account TOML never accepted
    any of these keys, so it needs no such check.
    """
    found = [
        f"  {_retired_key_shown(path)}: {reason}"
        for path, reason in _RETIRED_KEYS.items()
        if _has_path(payload, path)
    ]
    if found:
        raise ConfigError(
            f"{source}: delete these retired settings. No code ever read them, "
            "so removing them changes nothing:\n" + "\n".join(found)
        )


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
        If the per-queue TOML doesn't exist, fails to parse, sets a retired
        key (``[throttle.*]`` or one in ``_RETIRED_KEYS``), or the merged
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
        _reject_legacy_throttle(override, str(per_queue_toml))
        _reject_retired_keys(override, str(per_queue_toml))
        merged = _deep_merge(merged, override)

    try:
        return Settings.model_validate(merged)
    except ValidationError as exc:
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
    _reject_legacy_throttle(payload, str(path))
    try:
        return AccountPolicy.model_validate(payload)
    except ValidationError as exc:
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
