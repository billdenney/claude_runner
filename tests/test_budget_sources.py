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
