"""Tests for ``throttle.policy.resolve`` (ADR-0022).

Verifies queue + per-account merge semantics: every per-account field
that's ``None`` inherits the queue-wide value; explicit per-account
values override.
"""

from __future__ import annotations

from datetime import time

import pytest

from claude_task_runner.config.loader import load_settings
from claude_task_runner.config.schema import (
    AccountConcurrencyPolicy,
    AccountDispatchPctBand,
    AccountDispatchPctNight,
    AccountDispatchPctWeek,
    AccountDispatchPolicy,
    AccountPolicy,
)
from claude_task_runner.throttle.policy import (
    ResolvedBand,
    ResolvedNight,
    ResolvedWeek,
    resolve,
)


@pytest.fixture
def queue_settings():
    """Queue-side defaults loaded from the package TOML."""
    return load_settings()


class TestResolveDefaults:
    def test_account_with_no_overrides_inherits_queue(self, queue_settings) -> None:
        acct = AccountPolicy()
        r = resolve(queue_settings, acct, account_name="default")
        # Defaults from package TOML.
        assert r.account_name == "default"
        assert r.day == ResolvedBand(fivehr_slowdown_pct=40, fivehr_stop_pct=60)
        assert r.night.fivehr_slowdown_pct == 70
        assert r.night.fivehr_stop_pct == 90
        assert r.night.time_start == time(21, 0)
        assert r.night.time_end == time(6, 0)
        assert r.week.early_pct == 60
        assert r.week.eow_pct == 95
        assert r.week.eow_time_switch_s == 40 * 3600
        assert r.max_concurrency == 1  # AccountConcurrencyPolicy default
        assert r.timezone == ""

    def test_max_concurrency_explicit(self, queue_settings) -> None:
        acct = AccountPolicy(concurrency=AccountConcurrencyPolicy(max_concurrency=5))
        r = resolve(queue_settings, acct, account_name="work")
        assert r.max_concurrency == 5


class TestResolveOverrides:
    def test_day_override(self, queue_settings) -> None:
        acct = AccountPolicy(
            dispatch_pct=AccountDispatchPolicy(day=AccountDispatchPctBand(fivehr_slowdown_pct=20))
        )
        r = resolve(queue_settings, acct, account_name="a")
        # Slow overridden; stop inherits.
        assert r.day.fivehr_slowdown_pct == 20
        assert r.day.fivehr_stop_pct == 60

    def test_night_full_override(self, queue_settings) -> None:
        acct = AccountPolicy(
            dispatch_pct=AccountDispatchPolicy(
                night=AccountDispatchPctNight(
                    fivehr_slowdown_pct=50,
                    fivehr_stop_pct=75,
                    time_start="22:00",
                    time_end="07:00",
                )
            )
        )
        r = resolve(queue_settings, acct, account_name="a")
        assert r.night == ResolvedNight(
            fivehr_slowdown_pct=50,
            fivehr_stop_pct=75,
            time_start=time(22, 0),
            time_end=time(7, 0),
        )

    def test_week_override_only_eow_switch(self, queue_settings) -> None:
        acct = AccountPolicy(
            dispatch_pct=AccountDispatchPolicy(
                week=AccountDispatchPctWeek(eow_time_switch="24h"),
            )
        )
        r = resolve(queue_settings, acct, account_name="a")
        assert r.week == ResolvedWeek(early_pct=60, eow_pct=95, eow_time_switch_s=24 * 3600)

    def test_timezone_override(self, queue_settings) -> None:
        acct = AccountPolicy(
            dispatch_pct=AccountDispatchPolicy(timezone="America/Los_Angeles"),
        )
        r = resolve(queue_settings, acct, account_name="a")
        assert r.timezone == "America/Los_Angeles"


class TestResolvedFrozen:
    def test_resolved_policy_is_frozen(self, queue_settings) -> None:
        r = resolve(queue_settings, AccountPolicy(), account_name="default")
        with pytest.raises((AttributeError, Exception)):
            r.max_concurrency = 99  # type: ignore[misc]
