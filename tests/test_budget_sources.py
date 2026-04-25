from __future__ import annotations

import json
import subprocess
from datetime import UTC

import pytest

from claude_runner.budget.sources.ccusage import CCUsageError, CCUsageSource
from claude_runner.budget.sources.context_cmd import ContextCmdSource


@pytest.fixture
def fake_blocks_payload() -> dict:
    return {
        "blocks": [
            {"isActive": False, "totalTokens": 12345},
            {"isActive": True, "totalTokens": 87654, "endTime": "2026-04-19T18:00:00Z"},
        ]
    }


@pytest.fixture
def fake_daily_payload() -> dict:
    from datetime import datetime

    today = datetime.now(tz=UTC).date().isoformat()
    return {"daily": [{"date": today, "totalTokens": 50_000}]}


def test_ccusage_snapshot_parses_active_block(
    monkeypatch: pytest.MonkeyPatch, fake_blocks_payload: dict, fake_daily_payload: dict
) -> None:
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(args)
        payload = fake_blocks_payload if "blocks" in args else fake_daily_payload
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/ccusage")
    monkeypatch.setattr(subprocess, "run", fake_run)

    source = CCUsageSource()
    snap = source.snapshot()
    assert snap.used_5h == 87654
    assert snap.used_week >= 50_000
    assert snap.source == "ccusage"


def test_ccusage_returns_zero_when_binary_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: None)
    source = CCUsageSource()
    assert not source.available()
    snap = source.snapshot()
    assert snap.used_5h == 0
    assert snap.used_week == 0


def test_ccusage_falls_back_to_npx(
    monkeypatch: pytest.MonkeyPatch, fake_blocks_payload: dict, fake_daily_payload: dict
) -> None:
    # ccusage not installed, but npx is available: we should shell out via npx.
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/npx" if name == "npx" else None)

    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        payload = fake_blocks_payload if "blocks" in args else fake_daily_payload
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    source = CCUsageSource()
    assert source.available()
    snap = source.snapshot()
    assert snap.used_5h == 87654
    assert calls and calls[0][:3] == ["npx", "-y", "ccusage"]


def test_ccusage_raises_on_bad_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/ccusage")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            args=a, returncode=0, stdout="not json", stderr=""
        ),
    )
    source = CCUsageSource()
    with pytest.raises(CCUsageError):
        source._run("blocks")


def test_context_source_parses_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    output = "Current 5h: 1,234,567 tokens\nWeekly usage: 9,000,000 tokens"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            args=a, returncode=0, stdout=output, stderr=""
        ),
    )
    snap = ContextCmdSource().snapshot()
    assert snap.used_5h == 1_234_567
    assert snap.used_week == 9_000_000


def test_context_source_handles_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: None)
    snap = ContextCmdSource().snapshot()
    assert snap.used_5h == 0
    assert snap.used_week == 0


def test_api_headers_source_without_anthropic_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    sys.modules.pop("anthropic", None)
    # Force ImportError during the source call by shadowing sys.modules.
    monkeypatch.setitem(sys.modules, "anthropic", None)  # type: ignore[arg-type]
    from claude_runner.budget.sources.api_headers import ApiHeadersSource

    snap = ApiHeadersSource(itpm_budget=1000, weekly_budget=100000).snapshot()
    assert snap.used_5h == 0
    # Restore.
    sys.modules.pop("anthropic", None)


def test_api_headers_source_parses_rate_limit_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the probe-success path: parse remaining vs limit headers."""
    import sys
    import types

    fake_anthropic = types.ModuleType("anthropic")

    class _Resp:
        def __init__(self) -> None:
            self.headers = {
                "anthropic-ratelimit-input-tokens-remaining": "750",
                "anthropic-ratelimit-input-tokens-limit": "1000",
            }

    class _RawCreate:
        def create(self, **_: object) -> _Resp:
            return _Resp()

    class _Messages:
        with_raw_response = _RawCreate()

    class _Client:
        messages = _Messages()

    fake_anthropic.Anthropic = _Client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

    from claude_runner.budget.sources.api_headers import ApiHeadersSource

    snap = ApiHeadersSource(itpm_budget=1000, weekly_budget=100000).snapshot()
    assert snap.used_5h == 250  # limit 1000 - remaining 750
    assert snap.used_week == 0


def test_api_headers_source_handles_probe_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    fake_anthropic = types.ModuleType("anthropic")

    class _Raising:
        def create(self, **_: object) -> object:
            raise RuntimeError("network down")

    class _Messages:
        with_raw_response = _Raising()

    class _Client:
        messages = _Messages()

    fake_anthropic.Anthropic = _Client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

    from claude_runner.budget.sources.api_headers import ApiHeadersSource

    snap = ApiHeadersSource(itpm_budget=1000, weekly_budget=100000).snapshot()
    assert snap.used_5h == 0
    assert snap.source == "api_headers"


def test_api_headers_int_helper_parses_and_falls_back() -> None:
    from claude_runner.budget.sources.api_headers import _int

    assert _int("42") == 42
    assert _int(None) == 0
    assert _int("not-a-number") == 0


# ----- context_cmd source extra coverage ---------------------------------


def test_context_source_handles_subprocess_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")

    def raising(*_a: object, **_kw: object):
        raise OSError("no such executable")

    monkeypatch.setattr(subprocess, "run", raising)
    snap = ContextCmdSource().snapshot()
    assert snap.used_5h == 0
    assert snap.used_week == 0
    assert snap.source == "context"


def test_context_source_extract_regex_no_match() -> None:
    from claude_runner.budget.sources.context_cmd import _FIVE_H_RE, _extract

    # No match → returns 0 (hits the early return branch).
    assert _extract(_FIVE_H_RE, "unrelated text with no numbers") == 0


# ----- ccusage source extra coverage -------------------------------------


def test_ccusage_resolution_prefers_ccusage_when_custom_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/ccusage" if name == "ccusage" else None
    )
    source = CCUsageSource(binary="my-custom-ccusage")
    assert source.available()
    # Should have resolved to plain "ccusage" on PATH, not the custom name.
    assert source._cmd == ["ccusage"]


def test_ccusage_run_raises_when_subprocess_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/ccusage")

    def raising(args, **_kwargs):
        raise subprocess.CalledProcessError(returncode=1, cmd=args, stderr="oops")

    monkeypatch.setattr(subprocess, "run", raising)
    source = CCUsageSource()
    with pytest.raises(CCUsageError, match="failed"):
        source._run("blocks")


def test_ccusage_run_raises_when_subprocess_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/ccusage")

    def raising(args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=1)

    monkeypatch.setattr(subprocess, "run", raising)
    source = CCUsageSource()
    with pytest.raises(CCUsageError, match="timed out"):
        source._run("blocks")


def test_ccusage_run_raises_when_json_not_a_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/ccusage")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(
            args=a, returncode=0, stdout=json.dumps([1, 2, 3]), stderr=""
        ),
    )
    source = CCUsageSource()
    with pytest.raises(CCUsageError, match="unexpected JSON type"):
        source._run("blocks")


def test_ccusage_run_raises_when_command_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: None)
    source = CCUsageSource()
    with pytest.raises(CCUsageError, match="ccusage not found"):
        source._run("blocks")


def test_ccusage_active_block_handles_weird_shapes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-list blocks, non-dict rows, and no-active-block all yield zero."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/ccusage")

    def fake_run(args, **_kwargs):
        # "blocks" returns a dict instead of list -> 0
        if "blocks" in args:
            payload = {"blocks": "not-a-list"}
        else:
            # "daily" returns a list containing a non-dict -> row skipped
            payload = {"daily": ["not-a-dict", {"date": "not-a-date", "totalTokens": 5}]}
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    source = CCUsageSource()
    snap = source.snapshot()
    assert snap.used_5h == 0
    assert snap.used_week == 0


def test_ccusage_active_block_skips_non_dict_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/ccusage")

    def fake_run(args, **_kwargs):
        if "blocks" in args:
            payload = {"blocks": ["string-entry", {"isActive": False}]}
        else:
            payload = {"daily": []}
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    source = CCUsageSource()
    snap = source.snapshot()
    assert snap.used_5h == 0


def test_ccusage_extract_ignores_non_numeric_fallback_fields() -> None:
    """_extract_total_tokens skips fallback keys whose values aren't numeric."""
    from claude_runner.budget.sources.ccusage import _extract_total_tokens

    # No totalTokens; one fallback key has a string instead of a number — that
    # key is skipped, the numeric ones are summed.
    total = _extract_total_tokens(
        {
            "inputTokens": "not-a-number",
            "outputTokens": 10,
        }
    )
    assert total == 10


def test_ccusage_week_total_sums_input_output_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hit the fallback that sums inputTokens+outputTokens when no totalTokens key."""
    from datetime import datetime

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/ccusage")
    today = datetime.now(tz=UTC).date().isoformat()

    def fake_run(args, **_kwargs):
        if "blocks" in args:
            payload = {"blocks": []}
        else:
            payload = {
                "daily": [
                    {
                        "date": today,
                        "inputTokens": 100,
                        "outputTokens": 200,
                        "cacheCreationTokens": 50,
                        "cacheReadTokens": 1000,
                    }
                ]
            }
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    source = CCUsageSource()
    snap = source.snapshot()
    assert snap.used_week == 100 + 200 + 50 + 1000


def test_ccusage_parse_dt_and_date_return_none_on_garbage() -> None:
    from claude_runner.budget.sources.ccusage import _parse_date, _parse_dt

    assert _parse_dt(None) is None
    assert _parse_dt("not-a-timestamp") is None
    assert _parse_date(None) is None
    assert _parse_date("not-a-date") is None


def test_ccusage_active_block_uses_endsAt_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    """Covers the `endsAt` alias path when `endTime` is missing."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/ccusage")

    def fake_run(args, **_kwargs):
        if "blocks" in args:
            payload = {
                "blocks": [
                    {"active": True, "tokens": 42, "endsAt": "2026-05-01T00:00:00Z"},
                ]
            }
        else:
            payload = {"daily": []}
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    source = CCUsageSource()
    snap = source.snapshot()
    assert snap.used_5h == 42
    assert snap.next_5h_reset is not None


def test_ccusage_blocks_command_error_is_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the `blocks` subprocess call blows up, used_5h falls back to 0."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/ccusage")

    def fake_run(args, **_kwargs):
        # `blocks` call fails; `daily` succeeds with empty list.
        if "blocks" in args:
            raise subprocess.CalledProcessError(returncode=1, cmd=args, stderr="boom")
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=json.dumps({"daily": []}), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    source = CCUsageSource()
    snap = source.snapshot()
    assert snap.used_5h == 0
    assert snap.used_week == 0


def test_ccusage_daily_command_error_is_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/ccusage")

    def fake_run(args, **_kwargs):
        if "blocks" in args:
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout=json.dumps({"blocks": []}), stderr=""
            )
        raise subprocess.CalledProcessError(returncode=1, cmd=args, stderr="boom")

    monkeypatch.setattr(subprocess, "run", fake_run)
    source = CCUsageSource()
    snap = source.snapshot()
    assert snap.used_week == 0


def test_ccusage_week_total_skips_rows_before_anchor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rows dated before this week's Monday anchor are excluded from the sum."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/ccusage")

    def fake_run(args, **_kwargs):
        if "blocks" in args:
            payload = {"blocks": []}
        else:
            # Dates are from 2025 — well before any "current" week anchor.
            payload = {
                "daily": [
                    {"date": "2025-01-01", "totalTokens": 99_999},
                    {"date": "2025-01-02", "totalTokens": 88_888},
                ]
            }
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    source = CCUsageSource()
    snap = source.snapshot()
    assert snap.used_week == 0


def test_ccusage_week_total_rows_not_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/ccusage")

    def fake_run(args, **_kwargs):
        if "blocks" in args:
            payload = {"blocks": []}
        else:
            payload = {"daily": "not-a-list"}
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    source = CCUsageSource()
    snap = source.snapshot()
    assert snap.used_week == 0


# ----- ClaudeUsageSource --------------------------------------------------


from claude_runner.budget.sources.claude_usage import (  # noqa: E402
    ClaudeUsageError,
    ClaudeUsageSource,
)


@pytest.fixture
def fake_claude_usage_payload() -> dict:
    return {
        "five_hour": {
            "utilization": 42.5,
            "resets_at": "2026-04-24T20:00:00+00:00",
        },
        "seven_day": {
            "utilization": 80.0,
            "resets_at": "2026-04-30T00:00:00+00:00",
        },
        "seven_day_opus": None,
        "seven_day_sonnet": {"utilization": 4.0, "resets_at": None},
        "extra_usage": {
            "is_enabled": True,
            "monthly_limit": None,
            "used_credits": 100.0,
            "currency": "USD",
        },
    }


def test_claude_usage_snapshot_scales_percent_to_tokens(
    monkeypatch: pytest.MonkeyPatch, fake_claude_usage_payload: dict
) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/home/bill/.local/bin/claude-usage")

    def fake_run(args, **_kwargs):
        assert args[-1] == "json"
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps(fake_claude_usage_payload),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    source = ClaudeUsageSource(
        budget_5h_tokens=1_000_000,
        budget_weekly_tokens=10_000_000,
    )
    snap = source.snapshot()
    assert snap.source == "claude_usage"
    # 42.5% of 1_000_000 = 425_000
    assert snap.used_5h == 425_000
    # 80.0% of 10_000_000 = 8_000_000
    assert snap.used_week == 8_000_000
    assert snap.next_5h_reset is not None
    assert snap.next_5h_reset.year == 2026


def test_claude_usage_absolute_path_binary(
    monkeypatch: pytest.MonkeyPatch, fake_claude_usage_payload: dict
) -> None:
    # Absolute path bypasses shutil.which; should still invoke.
    captured_args: list[list[str]] = []

    def fake_run(args, **_kwargs):
        captured_args.append(list(args))
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps(fake_claude_usage_payload),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    source = ClaudeUsageSource(
        budget_5h_tokens=100,
        budget_weekly_tokens=100,
        binary="/home/bill/.local/bin/claude-usage",
    )
    assert source.available()
    source.snapshot()
    assert captured_args[0][0] == "/home/bill/.local/bin/claude-usage"


def test_claude_usage_missing_binary_returns_zeros(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: None)
    # disk_cache_path=None to opt out of the disk-cache fallback layer that
    # would otherwise serve real cached data on a developer machine where
    # ~/.claude/usage-cache.json may exist.
    source = ClaudeUsageSource(
        budget_5h_tokens=1_000,
        budget_weekly_tokens=10_000,
        disk_cache_path=None,
    )
    assert not source.available()
    snap = source.snapshot()
    assert snap.used_5h == 0
    assert snap.used_week == 0
    assert snap.source == "claude_usage"


def test_claude_usage_http_failure_yields_zeros(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude-usage")

    def fake_run(args, **_kwargs):
        raise subprocess.CalledProcessError(
            returncode=1, cmd=args, output="", stderr="API returned HTTP 401"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    source = ClaudeUsageSource(
        budget_5h_tokens=1_000,
        budget_weekly_tokens=10_000,
        disk_cache_path=None,
    )
    snap = source.snapshot()
    assert snap.used_5h == 0
    assert snap.used_week == 0


def test_claude_usage_handles_null_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    # Some windows come back null depending on plan; snapshot should treat as 0.
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude-usage")

    def fake_run(args, **_kwargs):
        payload = {
            "five_hour": None,
            "seven_day": {"utilization": None, "resets_at": None},
        }
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    source = ClaudeUsageSource(budget_5h_tokens=1_000_000, budget_weekly_tokens=10_000_000)
    snap = source.snapshot()
    assert snap.used_5h == 0
    assert snap.used_week == 0
    assert snap.next_5h_reset is None


def test_claude_usage_clamps_over_100_percent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude-usage")

    def fake_run(args, **_kwargs):
        payload = {
            "five_hour": {"utilization": 105.0, "resets_at": None},
            "seven_day": {"utilization": -2.0, "resets_at": None},
        }
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=json.dumps(payload), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    source = ClaudeUsageSource(budget_5h_tokens=1_000, budget_weekly_tokens=10_000)
    snap = source.snapshot()
    # 105% clamps to 100 → full budget
    assert snap.used_5h == 1_000
    # negative clamps to 0
    assert snap.used_week == 0


def test_claude_usage_non_json_output_yields_zeros(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude-usage")

    def fake_run(args, **_kwargs):
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout="not json at all", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    source = ClaudeUsageSource(
        budget_5h_tokens=1_000,
        budget_weekly_tokens=10_000,
        disk_cache_path=None,
    )
    snap = source.snapshot()
    assert snap.used_5h == 0
    assert snap.used_week == 0


def test_claude_usage_timeout_yields_zeros(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude-usage")

    def fake_run(args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=10)

    monkeypatch.setattr(subprocess, "run", fake_run)
    source = ClaudeUsageSource(
        budget_5h_tokens=1_000,
        budget_weekly_tokens=10_000,
        disk_cache_path=None,
    )
    snap = source.snapshot()
    assert snap.used_5h == 0
    assert snap.used_week == 0


def test_claude_usage_error_class_exists() -> None:
    # Smoke-test that the error class is exported for callers that want
    # to raise/reraise on explicit opt-in paths.
    assert issubclass(ClaudeUsageError, RuntimeError)


# ----- ClaudeUsageSource (cache + fallback layers) ------------------------


from datetime import UTC as _UTC  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402


def _make_clock(start: datetime):
    """Return a tickable clock for tests. Call clock.tick(seconds) to advance."""

    class _Clock:
        def __init__(self, t: datetime) -> None:
            self.t = t

        def __call__(self) -> datetime:
            return self.t

        def tick(self, seconds: float) -> None:
            self.t = self.t + timedelta(seconds=seconds)

    return _Clock(start)


def test_claude_usage_in_process_cache_serves_from_memory_within_ttl(
    monkeypatch: pytest.MonkeyPatch, fake_claude_usage_payload: dict
) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude-usage")
    call_count = {"n": 0}

    def fake_run(args, **_kwargs):
        call_count["n"] += 1
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps(fake_claude_usage_payload),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    clock = _make_clock(datetime(2026, 4, 25, 12, 0, 0, tzinfo=_UTC))
    source = ClaudeUsageSource(
        budget_5h_tokens=1_000_000,
        budget_weekly_tokens=10_000_000,
        cache_ttl_s=300,
        clock=clock,
    )

    snap1 = source.snapshot()
    assert call_count["n"] == 1
    assert snap1.source == "claude_usage"  # live

    clock.tick(60)  # well within the 300s TTL
    snap2 = source.snapshot()
    assert call_count["n"] == 1, "should have served from in-process cache"
    assert snap2.source == "claude_usage/memory_cache"
    # Same numbers
    assert snap2.used_5h == snap1.used_5h
    assert snap2.used_week == snap1.used_week

    clock.tick(300)  # now past the TTL
    snap3 = source.snapshot()
    assert call_count["n"] == 2, "TTL expired; should have shelled out again"
    assert snap3.source == "claude_usage"


def test_claude_usage_falls_back_to_disk_cache_on_subprocess_failure(
    monkeypatch: pytest.MonkeyPatch, fake_claude_usage_payload: dict, tmp_path
) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude-usage")

    def fake_run(args, **_kwargs):
        # Simulate the helper hitting HTTP 429.
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=args,
            output="",
            stderr="API returned HTTP 429",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    # Pre-populate disk cache with a real-looking payload.
    disk_cache = tmp_path / "usage-cache.json"
    disk_cache.write_text(json.dumps(fake_claude_usage_payload), encoding="utf-8")

    source = ClaudeUsageSource(
        budget_5h_tokens=1_000_000,
        budget_weekly_tokens=10_000_000,
        cache_ttl_s=0,  # force live calls so disk fallback path engages
        disk_cache_path=disk_cache,
    )
    snap = source.snapshot()
    assert snap.source == "claude_usage/disk_cache"
    # 42.5% of 1_000_000 from the fixture
    assert snap.used_5h == 425_000
    assert snap.used_week == 8_000_000


def test_claude_usage_disk_cache_used_after_429_warms_memory_cache(
    monkeypatch: pytest.MonkeyPatch, fake_claude_usage_payload: dict, tmp_path
) -> None:
    """After a disk-cache fallback, subsequent calls within TTL should hit
    the in-process cache rather than re-reading disk or re-shelling out."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude-usage")
    call_count = {"n": 0}

    def fake_run(args, **_kwargs):
        call_count["n"] += 1
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=args,
            output="",
            stderr="API returned HTTP 429",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    disk_cache = tmp_path / "usage-cache.json"
    disk_cache.write_text(json.dumps(fake_claude_usage_payload), encoding="utf-8")

    clock = _make_clock(datetime(2026, 4, 25, 12, 0, 0, tzinfo=_UTC))
    source = ClaudeUsageSource(
        budget_5h_tokens=1_000_000,
        budget_weekly_tokens=10_000_000,
        cache_ttl_s=300,
        disk_cache_path=disk_cache,
        clock=clock,
    )

    snap1 = source.snapshot()
    assert snap1.source == "claude_usage/disk_cache"
    assert call_count["n"] == 1

    clock.tick(60)
    snap2 = source.snapshot()
    # Should now be served from in-process cache, no further subprocess call.
    assert snap2.source == "claude_usage/memory_cache"
    assert call_count["n"] == 1


def test_claude_usage_no_cache_no_disk_emits_zeros(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude-usage")

    def fake_run(args, **_kwargs):
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=args,
            output="",
            stderr="API returned HTTP 401",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    disk_cache = tmp_path / "does-not-exist.json"  # never created
    source = ClaudeUsageSource(
        budget_5h_tokens=1_000_000,
        budget_weekly_tokens=10_000_000,
        cache_ttl_s=0,
        disk_cache_path=disk_cache,
    )
    snap = source.snapshot()
    assert snap.source == "claude_usage"
    assert snap.used_5h == 0
    assert snap.used_week == 0


def test_claude_usage_disk_cache_corrupt_json_falls_through_to_zeros(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude-usage")

    def fake_run(args, **_kwargs):
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=args,
            output="",
            stderr="API returned HTTP 429",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    disk_cache = tmp_path / "usage-cache.json"
    disk_cache.write_text("not valid json {{{", encoding="utf-8")

    source = ClaudeUsageSource(
        budget_5h_tokens=1_000_000,
        budget_weekly_tokens=10_000_000,
        cache_ttl_s=0,
        disk_cache_path=disk_cache,
    )
    snap = source.snapshot()
    assert snap.used_5h == 0
    assert snap.used_week == 0


def test_claude_usage_disk_cache_disabled_with_none(
    monkeypatch: pytest.MonkeyPatch, fake_claude_usage_payload: dict, tmp_path
) -> None:
    """Pass disk_cache_path=None to opt out of disk fallback entirely."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude-usage")

    def fake_run(args, **_kwargs):
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=args,
            output="",
            stderr="API returned HTTP 429",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    # Even though the file exists, disk_cache_path=None should ignore it.
    disk_cache = tmp_path / "usage-cache.json"
    disk_cache.write_text(json.dumps(fake_claude_usage_payload), encoding="utf-8")

    source = ClaudeUsageSource(
        budget_5h_tokens=1_000_000,
        budget_weekly_tokens=10_000_000,
        cache_ttl_s=0,
        disk_cache_path=None,
    )
    snap = source.snapshot()
    assert snap.used_5h == 0
    assert snap.used_week == 0


def test_claude_usage_cache_ttl_zero_disables_in_process_cache(
    monkeypatch: pytest.MonkeyPatch, fake_claude_usage_payload: dict
) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude-usage")
    call_count = {"n": 0}

    def fake_run(args, **_kwargs):
        call_count["n"] += 1
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps(fake_claude_usage_payload),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    source = ClaudeUsageSource(
        budget_5h_tokens=1_000_000,
        budget_weekly_tokens=10_000_000,
        cache_ttl_s=0,
    )
    source.snapshot()
    source.snapshot()
    source.snapshot()
    assert call_count["n"] == 3, "TTL=0 should bypass in-process cache"


def test_claude_usage_max_age_cap_rejects_stale_in_process_cache(
    monkeypatch: pytest.MonkeyPatch, fake_claude_usage_payload: dict
) -> None:
    """In-process cache older than max_cache_age_s must not be served,
    even if cache_ttl_s is larger."""
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude-usage")
    call_count = {"n": 0}

    def fake_run(args, **_kwargs):
        call_count["n"] += 1
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps(fake_claude_usage_payload),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    clock = _make_clock(datetime(2026, 4, 25, 12, 0, 0, tzinfo=_UTC))
    source = ClaudeUsageSource(
        budget_5h_tokens=1_000_000,
        budget_weekly_tokens=10_000_000,
        cache_ttl_s=3600,  # 1 hour soft TTL
        max_cache_age_s=900,  # 15 minutes hard cap
        disk_cache_path=None,
        clock=clock,
    )
    source.snapshot()
    assert call_count["n"] == 1

    clock.tick(14 * 60)  # 14 minutes — within 15-min cap
    source.snapshot()
    assert call_count["n"] == 1, "should still serve from in-process cache"

    clock.tick(2 * 60)  # now 16 minutes total — past cap
    source.snapshot()
    assert call_count["n"] == 2, "cap reached; must shell out fresh"


def test_claude_usage_disk_cache_rejected_when_file_too_old(
    monkeypatch: pytest.MonkeyPatch, fake_claude_usage_payload: dict, tmp_path
) -> None:
    """Disk cache older than max_cache_age_s must be ignored."""
    import os

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude-usage")

    def fake_run(args, **_kwargs):
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=args,
            output="",
            stderr="API returned HTTP 429",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    disk_cache = tmp_path / "usage-cache.json"
    disk_cache.write_text(json.dumps(fake_claude_usage_payload), encoding="utf-8")

    # Make the file 30 minutes old (mtime = now - 30 min). The default cap
    # is 15 min, so this should be rejected.
    now_ts = datetime(2026, 4, 25, 12, 0, 0, tzinfo=_UTC).timestamp()
    old_mtime = now_ts - 30 * 60
    os.utime(disk_cache, (old_mtime, old_mtime))

    clock = _make_clock(datetime(2026, 4, 25, 12, 0, 0, tzinfo=_UTC))
    source = ClaudeUsageSource(
        budget_5h_tokens=1_000_000,
        budget_weekly_tokens=10_000_000,
        cache_ttl_s=0,
        max_cache_age_s=900,
        disk_cache_path=disk_cache,
        clock=clock,
    )
    snap = source.snapshot()
    # Cache rejected as too old → zeros.
    assert snap.used_5h == 0
    assert snap.used_week == 0
    assert snap.source == "claude_usage"


def test_claude_usage_disk_cache_accepted_when_file_just_inside_cap(
    monkeypatch: pytest.MonkeyPatch, fake_claude_usage_payload: dict, tmp_path
) -> None:
    """A disk cache file just inside the 15-min cap should still be served."""
    import os

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude-usage")

    def fake_run(args, **_kwargs):
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=args,
            output="",
            stderr="API returned HTTP 429",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    disk_cache = tmp_path / "usage-cache.json"
    disk_cache.write_text(json.dumps(fake_claude_usage_payload), encoding="utf-8")

    # Make the file 10 minutes old — well within the 15-min cap.
    now_ts = datetime(2026, 4, 25, 12, 0, 0, tzinfo=_UTC).timestamp()
    recent_mtime = now_ts - 10 * 60
    os.utime(disk_cache, (recent_mtime, recent_mtime))

    clock = _make_clock(datetime(2026, 4, 25, 12, 0, 0, tzinfo=_UTC))
    source = ClaudeUsageSource(
        budget_5h_tokens=1_000_000,
        budget_weekly_tokens=10_000_000,
        cache_ttl_s=0,
        max_cache_age_s=900,
        disk_cache_path=disk_cache,
        clock=clock,
    )
    snap = source.snapshot()
    assert snap.source == "claude_usage/disk_cache"
    assert snap.used_5h == 425_000  # 42.5% of 1M
