"""Non-interactive OAuth token refresh.

The Claude Code CLI refreshes its OAuth bearer transparently on every
invocation. Third-party code that reads the bearer directly (notably
:class:`ApiUsageSource`) sees 401 once the access token expires.

Direct refresh via Anthropic's OAuth endpoint is undocumented and
fragile. The robust path is to make the CLI do the refresh as a side
effect: spawn ``claude`` non-interactively, let it perform any API
call, and the credentials file gets rewritten with a fresh token.

The existing :func:`usage.capture.capture` already does exactly this.
It drives ``claude /usage`` in a PTY (no operator interaction), the
TUI calls the OAuth-backed usage endpoint, and the CLI updates
``<config_dir>/.credentials.json`` on the way through. Calling it is
the simplest robust refresh — zero new spawn paths, zero new tokens
billed, ~30s per account.

Usage
-----
* As a library: :func:`refresh_oauth_token` returns the
  :class:`UsageReading` that proves the refresh succeeded.
* As an operator preflight:
  ``claude-task-runner usage refresh --queue ... --config ...`` —
  refreshes every configured account, reports per-account status.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from claude_task_runner.clock import Clock
from claude_task_runner.config.schema import UsageSettings
from claude_task_runner.usage.drift import (
    UsageCaptureSpawnError,
    UsageCaptureTimeout,
    UsageFormatDrift,
)
from claude_task_runner.usage.models import UsageReading
from claude_task_runner.usage.source import ClaudeUsageSource

logger = logging.getLogger(__name__)


class _AccountLike(Protocol):
    """Just the fields ``refresh_all_accounts`` needs from each entry.

    Declared structurally so the helper can be unit-tested with
    lightweight stand-ins rather than full :class:`AccountSettings`
    instances.
    """

    name: str
    config_dir: str


class OAuthRefreshFailed(RuntimeError):
    """Token refresh did not complete.

    Either the CLI couldn't be spawned (binary missing, perms),
    the PTY capture timed out before the OAuth call completed, or
    the credentials file was unwritable. In each case the underlying
    cause is chained as ``__cause__``.
    """


@dataclass(frozen=True)
class RefreshResult:
    """Outcome of one account's refresh attempt."""

    account: str
    """Account name from ``settings.accounts[*].name``."""

    config_dir: str
    """The ``CLAUDE_CONFIG_DIR`` whose credentials were refreshed."""

    success: bool
    """True iff the TTY capture completed without exception."""

    detail: str
    """Human-readable summary — utilization values on success, the
    exception class name and message on failure."""

    reading: UsageReading | None = None
    """The reading that proved the refresh, when successful. None on
    failure."""


def refresh_oauth_token(
    *,
    config_dir: str,
    settings: UsageSettings,
    captures_dir: Path,
    clock: Clock,
    claude_executable: str = "claude",
) -> UsageReading:
    """Spawn ``claude /usage`` non-interactively to refresh the OAuth bearer.

    Returns the parsed :class:`UsageReading`. The reading itself is
    a useful side-product — callers that want to confirm the refresh
    worked can check the timestamp.

    Raises :class:`OAuthRefreshFailed` on capture timeout, spawn
    error, or format drift. The underlying exception is chained.
    """
    src = ClaudeUsageSource(
        settings,
        clock,
        captures_dir=captures_dir,
        claude_executable=claude_executable,
        claude_config_dir=config_dir,
    )
    try:
        return src.read()
    except (UsageCaptureSpawnError, UsageCaptureTimeout, UsageFormatDrift) as exc:
        raise OAuthRefreshFailed(
            f"OAuth refresh via TTY /usage capture failed for "
            f"config_dir={config_dir!r}: {type(exc).__name__}: {exc}"
        ) from exc


def refresh_all_accounts(
    *,
    accounts: list[_AccountLike],
    settings: UsageSettings,
    captures_dir: Path,
    clock: Clock,
    claude_executable: str = "claude",
) -> list[RefreshResult]:
    """Refresh every account's OAuth bearer; return per-account results.

    Each ``accounts`` element must expose ``.name`` and ``.config_dir``
    (this matches :class:`AccountSettings`). Failures on one account
    do NOT stop the others — every account is attempted, and the
    caller decides how to surface the per-account status.

    The list is processed serially because each refresh spawns
    ``claude`` and the PTY interactions don't share resources well.
    """
    results: list[RefreshResult] = []
    for acct in accounts:
        try:
            reading = refresh_oauth_token(
                config_dir=acct.config_dir,
                settings=settings,
                captures_dir=captures_dir,
                clock=clock,
                claude_executable=claude_executable,
            )
            results.append(
                RefreshResult(
                    account=acct.name,
                    config_dir=acct.config_dir,
                    success=True,
                    detail=(
                        f"5h={reading.five_hour.utilization_pct}% "
                        f"weekly={reading.seven_day.utilization_pct}%"
                    ),
                    reading=reading,
                )
            )
        except OAuthRefreshFailed as exc:
            logger.warning("refresh failed for %s: %s", acct.name, exc)
            results.append(
                RefreshResult(
                    account=acct.name,
                    config_dir=acct.config_dir,
                    success=False,
                    detail=str(exc),
                    reading=None,
                )
            )
    return results
