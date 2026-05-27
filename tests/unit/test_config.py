"""Tests for the config loader and schema validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_task_runner.config.loader import (
    ConfigError,
    _deep_merge,
    load_defaults,
    load_settings,
)


class TestDeepMerge:
    def test_overrides_scalar(self) -> None:
        assert _deep_merge({"a": 1}, {"a": 2}) == {"a": 2}

    def test_recurses_nested_dicts(self) -> None:
        base = {"x": {"a": 1, "b": 2}}
        ovr = {"x": {"b": 99}}
        assert _deep_merge(base, ovr) == {"x": {"a": 1, "b": 99}}

    def test_lists_are_replaced_not_merged(self) -> None:
        base = {"items": [1, 2, 3]}
        ovr = {"items": [4]}
        assert _deep_merge(base, ovr) == {"items": [4]}

    def test_unrelated_keys_kept(self) -> None:
        base = {"a": 1, "b": 2}
        ovr = {"c": 3}
        assert _deep_merge(base, ovr) == {"a": 1, "b": 2, "c": 3}


class TestLoadDefaults:
    def test_loads_without_error(self) -> None:
        defaults = load_defaults()
        assert "usage" in defaults
        assert "dispatch_pct" in defaults
        assert defaults["dispatch_pct"]["day"]["fivehr_slowdown_pct"] == 40


class TestLoadSettings:
    def test_no_override(self) -> None:
        s = load_settings(None)
        assert s.dispatch_pct.day.fivehr_slowdown_pct == 40
        assert s.dispatch_pct.day.fivehr_stop_pct == 60
        assert s.usage.poll_interval_s == 60.0
        assert s.session.max_resume_attempts == 3

    def test_legacy_throttle_block_rejected(self, tmp_path: Path) -> None:
        """ADR-0022 retired ``[throttle.*]``; the loader raises with a
        migration hint so operators don't silently lose safety floors."""
        toml = tmp_path / "claude_runner.toml"
        toml.write_text(
            "[throttle.five_hour]\nband_full_dispatch_max_pct = 75\nband_slowdown_max_pct = 85\n"
        )
        with pytest.raises(ConfigError, match=r"\[throttle\.\*\]"):
            load_settings(toml)

    def test_override_replaces_value(self, tmp_path: Path) -> None:
        toml = tmp_path / "claude_runner.toml"
        toml.write_text("[dispatch_pct.day]\nfivehr_slowdown_pct = 75\nfivehr_stop_pct     = 85\n")
        s = load_settings(toml)
        assert s.dispatch_pct.day.fivehr_slowdown_pct == 75
        assert s.dispatch_pct.day.fivehr_stop_pct == 85
        # Untouched section keeps default
        assert s.session.max_resume_attempts == 3

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load_settings(tmp_path / "nope.toml")

    def test_invalid_toml_raises(self, tmp_path: Path) -> None:
        toml = tmp_path / "bad.toml"
        toml.write_text("not = valid = toml = at all\n")
        with pytest.raises(ConfigError, match="Invalid TOML"):
            load_settings(toml)

    def test_unknown_key_rejected(self, tmp_path: Path) -> None:
        toml = tmp_path / "extra.toml"
        toml.write_text("[usage]\nthere_is_no_such_key = 99\n")
        with pytest.raises(ConfigError, match="validation failed"):
            load_settings(toml)

    def test_out_of_range_value_rejected(self, tmp_path: Path) -> None:
        toml = tmp_path / "bad_range.toml"
        toml.write_text(
            "[dispatch_pct.day]\nfivehr_slowdown_pct = 250\n"  # > 100
        )
        with pytest.raises(ConfigError, match="validation failed"):
            load_settings(toml)
