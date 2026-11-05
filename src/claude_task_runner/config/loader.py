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

import errno
import logging
import os
import stat
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from claude_task_runner.config.linux_user import sudo_problem
from claude_task_runner.config.paths import ConfigDirError, expand_config_dir
from claude_task_runner.config.schema import (
    AccountPolicy,
    ResolvedAccount,
    Settings,
)
from claude_task_runner.once import first_time
from claude_task_runner.runner.effort_levels import RETIRED_EFFORT_LEVELS

logger = logging.getLogger(__name__)


class ConfigError(ValueError):
    """Raised when settings cannot be loaded or validated.

    ``transient`` tells the two kinds apart. ``False``: the configuration's
    content is wrong (a parse or validation error, a retired key, an empty
    account file, a ``~user`` that names no user), and trying again cannot
    help. ``True``: a file or directory could not be read (missing, denied,
    a symlink to nothing yet), which may clear up by itself, as when a
    mount comes up after boot.
    """

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


class AccountConfigDirError(ConfigError):
    """An account's ``config_dir`` cannot hold its ``runner-account.toml``:
    it does not exist, names no user's home, holds a NUL byte, or a path on
    the way to it is not a directory (:func:`load_account_policy`)."""


CONFIG_ERROR_EXIT_STATUS = 78
"""The exit status of a command that refuses to run because its
configuration's content is wrong (a :class:`ConfigError` that is not
``transient``): 78 is ``EX_CONFIG`` in sysexits.h. The systemd unit lists it
in ``RestartPreventExitStatus=``, so ``supervisor start`` refusing for this
reason stops the unit instead of restarting it until the start limit trips."""

TRANSIENT_CONFIG_ERROR_EXIT_STATUS = 1
"""The exit status for a ``transient`` :class:`ConfigError`: an ordinary
failure, which systemd's ``Restart=on-failure`` retries up to its start
limit, so a file on a mount that is not up yet recovers by itself."""


def config_error_exit_status(*, transient: bool) -> int:
    """:data:`TRANSIENT_CONFIG_ERROR_EXIT_STATUS` or :data:`CONFIG_ERROR_EXIT_STATUS`."""
    return TRANSIENT_CONFIG_ERROR_EXIT_STATUS if transient else CONFIG_ERROR_EXIT_STATUS


_CONTENT_ERRNOS = frozenset(
    {errno.ELOOP, errno.ENAMETOOLONG, errno.ENOTDIR, errno.EISDIR, errno.ENXIO}
)
"""``OSError`` errnos that retrying cannot fix, because the path itself is
wrong: a symlink loop, a name too long, a path through something that is
not a directory, a directory where a file belongs, a socket where a file
belongs (``open`` of a socket fails with ``ENXIO``)."""


def os_error_is_transient(exc: OSError) -> bool:
    """Whether reading a configuration file failed in a way that may clear up.

    The one classifier both loaders use. ``False`` for the errnos in
    :data:`_CONTENT_ERRNOS`. ``True`` for every other errno (``EACCES``,
    ``EPERM``, ``ENOENT``, ``EIO``, ``ESTALE``, ``EHOSTDOWN``, ``ENOTCONN``,
    ...): permissions get fixed and mounts come up, so a retry may succeed.
    """
    return exc.errno not in _CONTENT_ERRNOS


def _path_error(path: Path, exc: OSError) -> str | None:
    """What is wrong with ``path`` itself, worded the same by both loaders.

    A symlink whose target does not exist, named with that target, and a
    path through something that is not a directory, named as that
    ancestor. ``None`` for any other error.
    """
    if exc.errno == errno.ENOENT and _is_symlink(path):
        return f"{path} is a symlink to {os.readlink(path)}, which does not exist"
    if exc.errno == errno.ENOTDIR:
        return f"{_first_non_directory(path.parent)} is not a directory, so {path} cannot be read"
    return None


def _queue_toml_read_error(path: Path, exc: OSError) -> ConfigError:
    """The :class:`ConfigError` for the queue TOML failing to open or read."""
    message = _path_error(path, exc)
    if message is None:
        if exc.errno == errno.ENOENT:
            message = f"Settings file not found: {path}"
        elif exc.errno == errno.EISDIR:
            message = f"Settings file is a directory: {path}"
        else:
            message = f"Cannot read {path}: {exc}"
    return ConfigError(message, transient=os_error_is_transient(exc))


class _NotARegularFile(Exception):
    """A configuration path that names something other than a file or directory."""

    def __init__(self, kind: str) -> None:
        super().__init__(kind)
        self.kind = kind
        """``"a FIFO"``, ``"a character device"``, ...: how a message names it."""


_SPECIAL_KINDS = (
    (stat.S_ISFIFO, "a FIFO"),
    (stat.S_ISCHR, "a character device"),
    (stat.S_ISBLK, "a block device"),
    (stat.S_ISSOCK, "a socket"),
)


def _read_regular_file(path: Path) -> bytes:
    """The bytes of the regular file at ``path``, following symlinks.

    Opened without blocking and checked with ``fstat`` before it is read,
    so a FIFO with no writer cannot hang the caller (the supervisor's tick
    reads account files). Raises :class:`IsADirectoryError` for a
    directory, :class:`_NotARegularFile` for any other kind of file, and
    the ``OSError`` of a path that cannot be opened.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        mode = os.fstat(fd).st_mode
        if stat.S_ISDIR(mode):
            raise IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR), str(path))
        if not stat.S_ISREG(mode):
            raise _NotARegularFile(
                next((kind for test, kind in _SPECIAL_KINDS if test(mode)), "not a file")
            )
        with os.fdopen(fd, "rb", closefd=False) as fh:
            return fh.read()
    finally:
        os.close(fd)


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
    if renamed_models and first_time("retired_effort_levels", source):
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
        For every way the per-queue TOML can fail, so a caller handles one
        exception type. ``transient`` when it cannot be opened or read for
        a reason that may clear up (:func:`os_error_is_transient`); not
        ``transient`` when its path or content is wrong: a directory or
        anything else that is not a regular file (a FIFO, a device, a
        socket; refused before it is read, so a FIFO cannot block), a
        symlink loop, a path through a non-directory, not UTF-8 TOML, nested
        too deeply to parse, a retired key (``[throttle.*]`` or one in
        ``_RETIRED_KEYS``), or the merged settings fail schema validation.
    """
    try:
        return _load_settings(per_queue_toml)
    except RecursionError as exc:
        raise ConfigError(f"{per_queue_toml} is nested too deeply to load") from exc


def _load_settings(per_queue_toml: Path | None) -> Settings:
    merged = load_defaults()

    if per_queue_toml is not None:
        try:
            data = _read_regular_file(per_queue_toml)
        except OSError as exc:
            raise _queue_toml_read_error(per_queue_toml, exc) from exc
        except _NotARegularFile as exc:
            raise ConfigError(
                f"Settings file is {exc.kind}, not a regular file: {per_queue_toml}"
            ) from exc
        except ValueError as exc:
            # A NUL byte in the path: no file can have that name.
            raise ConfigError(f"Cannot read {per_queue_toml}: {exc}") from exc
        try:
            override = tomllib.loads(data.decode("utf-8"))
        except ValueError as exc:
            # TOMLDecodeError, UnicodeDecodeError, and the bare ValueError
            # tomllib raises for an integer past the interpreter's digit limit.
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
    whether or not the file exists. Raises :class:`ConfigError`, not
    ``transient``, when a leading ``~user`` names no user or the value
    holds a NUL byte, since no path can be built: the queue TOML refuses
    both at load, so it happens only when the user is removed while the
    supervisor runs, or for settings built without validation.
    """
    if not config_dir:
        return None
    try:
        return expand_config_dir(config_dir) / PER_ACCOUNT_TOML_NAME
    except ConfigDirError as exc:
        raise AccountConfigDirError(str(exc)) from exc


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

    A ``config_dir`` that exists with no such file → all defaults:
    ``max_concurrency=1`` and no ``[dispatch_pct.*]`` overrides, so
    :func:`throttle.policy.resolve` uses the queue's values. Empty
    config_dir → defaults (used for the synthesised legacy ``"default"``
    account before the operator declares an explicit config_dir).

    Raises :class:`ConfigError` otherwise, naming the file, or the path
    that is the problem (:class:`AccountConfigDirError` when the problem
    is the ``config_dir``, not the file). ``transient`` (it may clear up by itself): the
    ``config_dir`` does not exist (a mount not up yet), the file is a
    symlink to a file that does not exist yet, or it cannot be opened or
    read for another reason :func:`os_error_is_transient` counts as
    passing (denied, an I/O or stale-mount error). Not ``transient`` (the
    path or content is wrong): the ``config_dir`` names no user's home or
    holds a NUL byte, a path on the way is not a directory, a symlink
    loop, a name too long, the file is a directory or anything else that is
    not a regular file (a FIFO, a device, a socket), is empty (zero bytes,
    usually a save cut short, so not read as "use the defaults"), is not
    UTF-8 TOML, is nested too deeply, sets the retired ``[throttle.*]``
    table or fails validation. :func:`resolve_accounts` turns the error
    into a :class:`BrokenAccount` instead of letting it propagate.
    """
    path = per_account_toml_path(config_dir)
    if path is None:
        return AccountPolicy()
    try:
        data = _read_regular_file(path)
    except _NotARegularFile as exc:
        raise ConfigError(f"{path} is {exc.kind}, not a regular file") from exc
    except OSError as exc:
        message = _path_error(path, exc)
        if message is None and exc.errno == errno.ENOENT:
            # exists, not lexists: a config_dir symlinked to a mount that is
            # not up yet does not exist either.
            if not os.path.exists(path.parent):
                raise AccountConfigDirError(
                    f"config_dir {path.parent} does not exist", transient=True
                ) from None
            return AccountPolicy()
        error = AccountConfigDirError if exc.errno == errno.ENOTDIR else ConfigError
        raise error(
            message or f"Cannot read {path}: {exc}", transient=os_error_is_transient(exc)
        ) from exc
    if not data:
        raise ConfigError(
            f"{path} is empty; delete it for the defaults, or write the settings it should hold"
        )
    try:
        payload = tomllib.loads(data.decode("utf-8"))
    except ValueError as exc:
        # TOMLDecodeError, UnicodeDecodeError, and the bare ValueError
        # tomllib raises for an integer past the interpreter's digit limit.
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc
    except RecursionError as exc:
        raise ConfigError(f"{path} is nested too deeply to load") from exc
    _reject_legacy_throttle(payload, str(path))
    try:
        return AccountPolicy.model_validate(payload)
    except ValidationError as exc:
        raise ConfigError(f"Per-account policy validation failed for {path}: {exc}") from exc


def _first_non_directory(directory: Path) -> Path:
    """The path from the root to ``directory`` that exists but is not a directory.

    ``directory`` itself when no single one can be named (it changed
    meanwhile), so the message still names a path on the way.
    """
    for candidate in (*reversed(directory.parents), directory):
        if os.path.exists(candidate) and not os.path.isdir(candidate):
            return candidate
    return directory


def _is_symlink(path: Path) -> bool:
    try:
        return path.is_symlink()
    except OSError:
        return False


BrokenCause = Literal["file", "config_dir", "sudo"]
"""What keeps a :class:`BrokenAccount` from taking tasks."""


@dataclass(frozen=True)
class BrokenAccount:
    """A declared account that cannot take tasks, for the :attr:`cause` it
    gives: its ``runner-account.toml`` does not load, its ``config_dir``
    cannot hold one, or its ``linux_user`` needs ``sudo``, which is not on
    PATH.

    It takes no new tasks until the cause is fixed. No policy stands in
    for a file that does not load, neither defaults nor the last one that
    loaded, so a half-edited file is never read as something its owner did
    not write (ADR-0035).
    """

    name: str
    path: str
    """The account's ``runner-account.toml``, as written when no path
    could be built from its ``config_dir``; ``""`` for an empty
    ``config_dir``. Whatever the cause, so a status line can name it."""
    error: str
    """What is wrong: the :class:`ConfigError` message, which names the
    file or the ``config_dir``, or the ``sudo`` problem."""
    cause: BrokenCause
    """``"file"``: the ``runner-account.toml`` does not load.
    ``"config_dir"``: the ``config_dir`` cannot hold one
    (:class:`AccountConfigDirError`). ``"sudo"``: the ``linux_user`` needs
    ``sudo``, which is not on PATH."""
    transient: bool = False
    """:attr:`ConfigError.transient`: whether it may clear up by itself."""


@dataclass(frozen=True)
class AccountResolution:
    """What :func:`resolve_accounts` found, in declaration order.

    Every declared account is in exactly one of the two tuples.
    """

    accounts: tuple[ResolvedAccount, ...]
    """The accounts whose policy loaded (or whose config_dir has no such file)."""
    broken: tuple[BrokenAccount, ...]
    """The accounts that cannot take tasks: their file or config_dir does
    not load, or they need ``sudo``, which is not on PATH."""

    @property
    def policies(self) -> dict[str, AccountPolicy]:
        """Each loaded account's policy, keyed by name."""
        return {acct.name: acct.policy for acct in self.accounts}

    @property
    def broken_names(self) -> frozenset[str]:
        return frozenset(b.name for b in self.broken)

    @property
    def only_transient(self) -> bool:
        """Whether every broken account may clear up by itself (and there is one)."""
        return bool(self.broken) and all(b.transient for b in self.broken)


def resolve_accounts(settings: Settings) -> AccountResolution:
    """Compose each account's queue-side declaration with its per-account policy.

    Walks ``settings.accounts`` in declaration order; for each entry,
    reads ``<config_dir>/runner-account.toml`` via
    :func:`load_account_policy`. An account whose file loads, or whose
    ``config_dir`` has no such file (the defaults apply), becomes a
    :class:`ResolvedAccount`; any other becomes a :class:`BrokenAccount`,
    and so does one whose ``linux_user`` needs ``sudo`` when ``sudo`` is
    not on PATH (:func:`config.linux_user.sudo_problem`), a content
    error: either way the account cannot start a worker.
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
                    cause="config_dir" if isinstance(exc, AccountConfigDirError) else "file",
                    transient=exc.transient,
                )
            )
            continue
        problem = sudo_problem(acct.linux_user)
        if problem is not None:
            broken.append(
                BrokenAccount(
                    name=acct.name,
                    path=per_account_toml_shown(acct.config_dir) or "",
                    error=problem,
                    cause="sudo",
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
