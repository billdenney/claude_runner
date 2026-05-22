"""Tests for the non-interactive OAuth token refresh helper.

The refresh primitive delegates to :class:`ClaudeUsageSource`, which
spawns ``claude /usage`` via pexpect. The CLI rewrites the credentials
file as a side effect of the OAuth-backed call. These tests mock the
inner source's ``read()`` so the suite doesn't actually spawn claude.

Coverage:
* ``refresh_oauth_token`` returns the inner reading on success.
* Each documented inner exception (spawn, timeout, drift) is wrapped
  in :class:`OAuthRefreshFailed` with the original chained as ``__cause__``.
* ``refresh_all_accounts`` continues past per-account failures and
  reports per-account status accurately.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_task_runner.clock import RealClock
from claude_task_runner.config.loader import load_settings
from claude_task_runner.usage.drift import (
    UsageCaptureSpawnError,
    UsageCaptureTimeout,
    UsageFormatDrift,
)
from claude_task_runner.usage.models import UsageReading, WindowReading
from claude_task_runner.usage.oauth_refresh import (
    OAuthRefreshFailed,
    refresh_all_accounts,
    refresh_oauth_token,
)


@dataclass
class _FakeAccount:
    name: str
    config_dir: str


def _reading() -> UsageReading:
    return UsageReading(
        captured_at=datetime(2026, 5, 22, tzinfo=UTC),
        five_hour=WindowReading(
            utilization_pct=8,
            resets_at_raw="2:40am (UTC)",
            resets_at=datetime(2026, 5, 22, 17, tzinfo=UTC),
        ),
        seven_day=WindowReading(
            utilization_pct=76,
            resets_at_raw="May 29, 11am (UTC)",
            resets_at=datetime(2026, 5, 29, 11, tzinfo=UTC),
        ),
    )


def test_refresh_returns_inner_reading_on_success(tmp_path: Path) -> None:
    settings = load_settings(None).usage
    with patch(
        "claude_task_runner.usage.oauth_refresh.ClaudeUsageSource.read",
        return_value=_reading(),
    ):
        out = refresh_oauth_token(
            config_dir="/home/bill/.claude_personal",
            settings=settings,
            captures_dir=tmp_path,
            clock=RealClock(),
        )
    assert out.five_hour.utilization_pct == 8
    assert out.seven_day.utilization_pct == 76


@pytest.mark.parametrize(
    "inner_exc",
    [
        UsageCaptureSpawnError("claude not on PATH"),
        UsageCaptureTimeout("TUI did not become ready"),
        UsageFormatDrift("two Resets lines not found"),
    ],
)
def test_refresh_wraps_inner_exceptions(tmp_path: Path, inner_exc: Exception) -> None:
    settings = load_settings(None).usage
    with (
        patch(
            "claude_task_runner.usage.oauth_refresh.ClaudeUsageSource.read",
            side_effect=inner_exc,
        ),
        pytest.raises(OAuthRefreshFailed) as exc_info,
    ):
        refresh_oauth_token(
            config_dir="/home/bill/.claude",
            settings=settings,
            captures_dir=tmp_path,
            clock=RealClock(),
        )
    assert exc_info.value.__cause__ is inner_exc


def test_refresh_all_continues_past_failures(tmp_path: Path) -> None:
    """One account's failure must not abort the others."""
    settings = load_settings(None).usage
    accounts = [
        _FakeAccount(name="personal", config_dir="/home/bill/.claude_personal"),
        _FakeAccount(name="work", config_dir="/home/bill/.claude"),
    ]
    call_count = {"n": 0}

    def _alternate(*_args, **_kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return _reading()
        raise UsageCaptureTimeout("simulated timeout")

    with patch(
        "claude_task_runner.usage.oauth_refresh.ClaudeUsageSource.read",
        side_effect=_alternate,
    ):
        results = refresh_all_accounts(
            accounts=accounts,
            settings=settings,
            captures_dir=tmp_path,
            clock=RealClock(),
        )

    assert len(results) == 2
    by_name = {r.account: r for r in results}
    assert by_name["personal"].success is True
    assert by_name["personal"].reading is not None
    assert by_name["work"].success is False
    assert "timeout" in by_name["work"].detail.lower()
    assert by_name["work"].reading is None


def test_refresh_all_empty_accounts_returns_empty_list(tmp_path: Path) -> None:
    settings = load_settings(None).usage
    results = refresh_all_accounts(
        accounts=[],
        settings=settings,
        captures_dir=tmp_path,
        clock=RealClock(),
    )
    assert results == []
