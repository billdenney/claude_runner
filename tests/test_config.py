from __future__ import annotations

from pathlib import Path

import pytest

from claude_runner.config import Settings, load_settings


def test_defaults() -> None:
    s = Settings()
    assert s.regime == "pro_max"
    assert s.plan in {"pro", "max5", "max20", "team", "custom"}
    assert s.max_concurrency >= 1
    assert s.resolved_budget_5h() > 0
    assert s.resolved_budget_weekly() > 0


def test_file_values_loaded(tmp_path: Path) -> None:
    (tmp_path / "claude_runner.toml").write_text(
        'regime = "api"\nplan = "pro"\nmax_concurrency = 2\n',
        encoding="utf-8",
    )
    s = load_settings(tmp_path)
    assert s.regime == "api"
    assert s.plan == "pro"
    assert s.max_concurrency == 2


def test_env_var_overrides_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "claude_runner.toml").write_text("max_concurrency = 2\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_RUNNER_MAX_CONCURRENCY", "9")
    s = load_settings(tmp_path)
    assert s.max_concurrency == 9


def test_rolling_window_validator() -> None:
    with pytest.raises(ValueError):
        Settings(failure_rolling_window=2, failure_rate_min_samples=5)


def test_load_settings_with_no_file(tmp_path: Path) -> None:
    s = load_settings(tmp_path)
    assert s is not None
    # Same as defaults when no file.
    assert s.default_model


def test_explicit_budget_overrides_plan() -> None:
    s = Settings(plan="pro", budget_5h_tokens=123_456, budget_weekly_tokens=7_777_777)
    assert s.resolved_budget_5h() == 123_456
    assert s.resolved_budget_weekly() == 7_777_777
