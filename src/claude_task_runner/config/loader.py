"""Settings loader: package defaults overlaid by per-queue TOML.

Two-layer load:

* :func:`load_settings` returns the queue-side :class:`Settings` (the
  classic defaults + ``claude_runner.toml`` merge).
* :func:`resolve_accounts` walks ``settings.accounts`` and reads each
  account's own ``<config_dir>/runner-account.toml`` for the per-
  account dispatch policy. Composes a :class:`ResolvedAccount` per
  account, or a :class:`BrokenAccount` for one whose file does not load.
  Missing per-account file → defaults.

The split keeps the queue config slim (it only declares *which*
accounts to use) while each account owner controls their own
``max_concurrency`` and throttle bands inside their own Claude
config dir.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from claude_task_runner.config.paths import ConfigDirError, expand_config_dir
from claude_task_runner.config.schema import (
    AccountPolicy,
    ResolvedAccount,
    Settings,
)
from claude_task_runner.runner.effort_levels import RETIRED_EFFORT_LEVELS

logger = logging.getLogger(__name__)


class ConfigError(ValueError):
    """Raised when settings cannot be loaded or validated."""


CONFIG_ERROR_EXIT_STATUS = 78
"""The exit status of ``supervisor start`` when it refuses to start because
its configuration does not load: the queue TOML, or any account's
``runner-account.toml``. 78 is ``EX_CONFIG`` in sysexits.h. The systemd unit
lists it in ``RestartPreventExitStatus=``, so a configuration error stops
the unit instead of restarting it until the start limit trips."""


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
    ("usage", "healthcheck_interval_s"): (
        "was the period of a background /usage healthcheck that nothing ever "
        "scheduled; `claude-task-runner usage healthcheck` runs one on demand"
    ),
    ("usage", "suspicious_delta_pct"): (
        "tuned a utilization monotonicity check that nothing ever called"
    ),
    ("session", "resume_fail_fast_s"): (
        "timed a fast fall-through from a failed --resume that was never built; "
        "a resume is retried until [session].max_resume_attempts, then goes fresh "
        "(ADR-0005)"
    ),
    ("ema",): (
        "the per-task-type EMA (ADR-0011) was never wired into dispatch and has "
        "been removed; concurrency comes from [concurrency] and each account's "
        "max_concurrency, throttled by [dispatch_pct.*]"
    ),
    ("failure_classifier", "environmental_patterns"): (
        "matched failures for runner.retry.classify, which nothing ever called; "
        "every failed run is retried until "
        "[failure_classifier].failure_circuit_breaker_threshold (ADR-0012)"
    ),
    ("failure_classifier", "operator_patterns"): (
        "see [failure_classifier].environmental_patterns"
    ),
    ("failure_classifier", "task_patterns"): ("see [failure_classifier].environmental_patterns"),
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


_warned_retired_effort_sources: set[str] = set()
"""Queue TOMLs already warned about for a retired effort spelling. The
supervisor re-reads its TOML on every SIGHUP, so each file is warned about
once per process."""


def _rename_retired_effort_levels(payload: dict[str, Any], source: str) -> None:
    """Rename retired spellings in ``payload``'s ``[effort_levels]`` in place.

    Each old spelling in :data:`~claude_task_runner.runner.effort_levels.RETIRED_EFFORT_LEVELS`
    (``extra_high``) becomes the name the ``claude`` CLI accepts (``xhigh``),
    since the CLI ignores a name it does not know and runs at its default
    effort. A list that then names a level twice keeps it once. Warns once
    per file per process, naming the models to update. Called on the
    queue TOML before defaults are merged; the packaged defaults use the
    current names, which ``tests/unit/test_config.py`` checks.
    Anything that is not a table of lists is left for the schema to reject.
    """
    table = payload.get("effort_levels")
    if not isinstance(table, dict):
        return
    renamed_models: list[str] = []
    for model, levels in table.items():
        if not isinstance(levels, list) or not any(
            isinstance(level, str) and level in RETIRED_EFFORT_LEVELS for level in levels
        ):
            continue
        renamed = [
            RETIRED_EFFORT_LEVELS.get(level, level) if isinstance(level, str) else level
            for level in levels
        ]
        table[model] = list(dict.fromkeys(renamed))
        renamed_models.append(model)
    if renamed_models and source not in _warned_retired_effort_sources:
        _warned_retired_effort_sources.add(source)
        spellings = ", ".join(f"{old!r} as {new!r}" for old, new in RETIRED_EFFORT_LEVELS.items())
        logger.warning(
            "%s: reading [effort_levels] %s, the claude CLI's name, for %s; "
            "update the file to the new name",
            source,
            spellings,
            ", ".join(repr(model) for model in renamed_models),
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
        _rename_retired_effort_levels(override, str(per_queue_toml))
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
    whether or not the file exists. Raises :class:`ConfigError` when a
    leading ``~user`` names no user, since no path can be built: the queue
    TOML refuses that at load, so it happens only when the user is removed
    while the supervisor runs.
    """
    if not config_dir:
        return None
    try:
        return expand_config_dir(config_dir) / PER_ACCOUNT_TOML_NAME
    except ConfigDirError as exc:
        raise ConfigError(str(exc)) from exc


def per_account_toml_shown(config_dir: str) -> str | None:
    """:func:`per_account_toml_path` as a string, as written when it cannot
    be built, or ``None`` for an empty ``config_dir``."""
    try:
        path = per_account_toml_path(config_dir)
    except ConfigError:
        return f"{config_dir}/{PER_ACCOUNT_TOML_NAME}"
    return None if path is None else str(path)


def load_account_policy(config_dir: str) -> AccountPolicy:
    """Read ``<config_dir>/runner-account.toml`` and return the policy.

    An absent file → all defaults: ``max_concurrency=1`` and no
    ``[dispatch_pct.*]`` overrides, so :func:`throttle.policy.resolve`
    uses the queue's values. A missing ``config_dir``, or one that is not
    a directory, counts as an absent file. Empty config_dir → defaults
    (used for the synthesised legacy ``"default"`` account before the
    operator declares an explicit config_dir).

    Raises :class:`ConfigError` naming the file when it is there but does
    not load: it cannot be read, is a symlink to nothing, is empty (zero
    bytes), is not UTF-8 TOML, sets the retired ``[throttle.*]`` table or
    fails validation. An empty file or a dangling symlink is usually a
    file caught mid-save, so it is not read as "use the defaults".
    :func:`resolve_accounts` turns the error into a :class:`BrokenAccount`
    instead of letting it propagate.
    """
    path = per_account_toml_path(config_dir)
    if path is None:
        return AccountPolicy()
    try:
        with path.open("rb") as fh:
            data = fh.read()
    except (FileNotFoundError, NotADirectoryError):
        if _is_symlink(path):
            raise ConfigError(
                f"{path} is a symlink to {os.readlink(path)}, which does not exist"
            ) from None
        return AccountPolicy()
    except OSError as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from exc
    if not data:
        raise ConfigError(
            f"{path} is empty; delete it for the defaults, or write the settings it should hold"
        )
    try:
        payload = tomllib.loads(data.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc
    _reject_legacy_throttle(payload, str(path))
    try:
        return AccountPolicy.model_validate(payload)
    except ValidationError as exc:
        raise ConfigError(f"Per-account policy validation failed for {path}: {exc}") from exc


def _is_symlink(path: Path) -> bool:
    try:
        return path.is_symlink()
    except OSError:
        return False


@dataclass(frozen=True)
class BrokenAccount:
    """A declared account whose ``runner-account.toml`` does not load.

    It takes no new tasks until the file loads again. No policy stands in
    for the file, neither defaults nor the last one that loaded, so a
    half-edited file is never read as something its owner did not write
    (ADR-0035).
    """

    name: str
    path: str
    """The ``runner-account.toml`` that failed."""
    error: str
    """The :class:`ConfigError` message, which names the file."""


@dataclass(frozen=True)
class AccountResolution:
    """What :func:`resolve_accounts` found, in declaration order.

    Every declared account is in exactly one of the two tuples.
    """

    accounts: tuple[ResolvedAccount, ...]
    """The accounts whose policy loaded (or whose file is missing)."""
    broken: tuple[BrokenAccount, ...]
    """The accounts whose file exists but does not load."""

    @property
    def policies(self) -> dict[str, AccountPolicy]:
        """Each loaded account's policy, keyed by name."""
        return {acct.name: acct.policy for acct in self.accounts}

    @property
    def broken_names(self) -> frozenset[str]:
        return frozenset(b.name for b in self.broken)


def resolve_accounts(settings: Settings) -> AccountResolution:
    """Compose each account's queue-side declaration with its per-account policy.

    Walks ``settings.accounts`` in declaration order; for each entry,
    reads ``<config_dir>/runner-account.toml`` via
    :func:`load_account_policy`. An account whose file loads, or is
    missing (the defaults apply), becomes a :class:`ResolvedAccount`; one
    whose file exists but does not load becomes a :class:`BrokenAccount`.
    Never raises for a per-account file, so one broken file cannot stop
    the supervisor, a dispatch or a CLI listing: each caller decides what a
    broken account means for it.
    """
    resolved: list[ResolvedAccount] = []
    broken: list[BrokenAccount] = []
    for acct in settings.accounts:
        try:
            policy = load_account_policy(acct.config_dir)
        except ConfigError as exc:
            broken.append(
                BrokenAccount(
                    name=acct.name,
                    path=per_account_toml_shown(acct.config_dir) or "",
                    error=str(exc),
                )
            )
            continue
        resolved.append(
            ResolvedAccount(
                name=acct.name,
                config_dir=acct.config_dir,
                linux_user=acct.linux_user,
                policy=policy,
            )
        )
    return AccountResolution(accounts=tuple(resolved), broken=tuple(broken))
