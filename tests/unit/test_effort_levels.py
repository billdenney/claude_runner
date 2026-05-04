"""Tests for runner/effort_levels.py — TOML-driven validation per model."""

from __future__ import annotations

import pytest

from claude_task_runner.runner.effort_levels import (
    UnknownEffortLevel,
    UnknownModel,
    accepted_efforts,
    accepted_models,
    validate_effort,
)

LEVELS = {
    "claude-opus-4-7": ["low", "medium", "high", "max", "extra_high"],
    "claude-sonnet-4-6": ["low", "medium", "high"],
    "claude-haiku-4-5": ["low", "medium", "high"],
}


class TestAcceptedEfforts:
    def test_returns_list(self) -> None:
        assert accepted_efforts("claude-opus-4-7", LEVELS) == [
            "low",
            "medium",
            "high",
            "max",
            "extra_high",
        ]

    def test_returns_copy_not_reference(self) -> None:
        out = accepted_efforts("claude-opus-4-7", LEVELS)
        out.append("forged")
        # Original unchanged
        assert "forged" not in LEVELS["claude-opus-4-7"]

    def test_unknown_model_raises(self) -> None:
        with pytest.raises(UnknownModel) as exc_info:
            accepted_efforts("claude-foo-9-9", LEVELS)
        assert exc_info.value.model == "claude-foo-9-9"
        assert "claude-opus-4-7" in str(exc_info.value)


class TestAcceptedModels:
    def test_includes_all_with_efforts(self) -> None:
        assert set(accepted_models(LEVELS)) == set(LEVELS)

    def test_excludes_empty_lists(self) -> None:
        levels = {**LEVELS, "claude-empty-0-0": []}
        out = accepted_models(levels)
        assert "claude-empty-0-0" not in out
        assert "claude-opus-4-7" in out


class TestValidateEffort:
    def test_accepted_passes(self) -> None:
        # Should not raise.
        validate_effort("claude-opus-4-7", "max", LEVELS)
        validate_effort("claude-sonnet-4-6", "high", LEVELS)

    def test_unknown_effort_for_known_model_raises(self) -> None:
        with pytest.raises(UnknownEffortLevel) as exc_info:
            validate_effort("claude-sonnet-4-6", "max", LEVELS)
        assert exc_info.value.model == "claude-sonnet-4-6"
        assert exc_info.value.effort == "max"
        assert exc_info.value.accepted == ["low", "medium", "high"]

    def test_unknown_model_raises_with_no_accepted(self) -> None:
        with pytest.raises(UnknownEffortLevel) as exc_info:
            validate_effort("claude-newmodel-99", "high", LEVELS)
        assert exc_info.value.accepted is None
        msg = str(exc_info.value)
        assert "no effort levels configured" in msg
        assert "claude-newmodel-99" in msg

    def test_error_message_lists_accepted(self) -> None:
        with pytest.raises(UnknownEffortLevel) as exc_info:
            validate_effort("claude-haiku-4-5", "extra_high", LEVELS)
        msg = str(exc_info.value)
        # Accepted set is sorted in the message for stability
        assert "['high', 'low', 'medium']" in msg

    def test_case_sensitive(self) -> None:
        # We don't normalize case — Anthropic's strings are lowercase.
        with pytest.raises(UnknownEffortLevel):
            validate_effort("claude-opus-4-7", "MAX", LEVELS)

    def test_works_with_settings_dict(self, default_settings) -> None:
        # Verify the schema-loaded settings can be used directly.
        validate_effort("claude-opus-4-7", "high", default_settings.effort_levels)
