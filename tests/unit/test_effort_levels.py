"""Tests for runner/effort_levels.py — TOML-driven validation per model."""

from __future__ import annotations

import pytest

from claude_task_runner.queue.schema import Task
from claude_task_runner.runner import readiness
from claude_task_runner.runner.effort_levels import (
    HOLD_REASON_PREFIX,
    UnknownEffortLevel,
    hold_reason,
    is_hold_reason,
    validate_effort,
)

LEVELS = {
    "claude-opus-4-7": ["low", "medium", "high", "max", "extra_high"],
    "claude-sonnet-4-6": ["low", "medium", "high"],
    "claude-haiku-4-5": ["low", "medium", "high"],
}


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

    def test_unknown_model_message_shows_the_entry_to_add(self) -> None:
        """The fix is a key under [effort_levels], not a table of its own:
        ``[effort_levels.'<model>']`` (the old hint) would define a sub-table,
        which the settings schema rejects."""
        with pytest.raises(UnknownEffortLevel) as exc_info:
            validate_effort("claude-newmodel-99", "high", LEVELS)
        assert str(exc_info.value) == (
            "model 'claude-newmodel-99' has no effort levels configured; "
            'add "claude-newmodel-99" = [<levels>] under [effort_levels] in '
            "claude_runner.toml or use a configured model"
        )

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

    def test_task_defaults_are_accepted_by_the_package_defaults(self, default_settings) -> None:
        """A task YAML that leaves model and effort out must validate, or the
        supervisor would park every such task. Changing ``Task.model``'s
        default without an [effort_levels] entry for it fails here first."""
        task = Task(id="t", title="t", prompt="p")
        validate_effort(task.model, task.effort, default_settings.effort_levels)


class TestHoldReason:
    def test_reason_is_the_prefix_and_the_error(self) -> None:
        exc = UnknownEffortLevel("claude-sonnet-4-6", "max", accepted=["low", "medium", "high"])
        assert hold_reason(exc) == (
            "invalid effort: effort 'max' not in accepted set for model "
            "'claude-sonnet-4-6': ['high', 'low', 'medium']"
        )
        assert hold_reason(exc).startswith(HOLD_REASON_PREFIX)

    def test_recognises_its_own_output(self) -> None:
        assert is_hold_reason(hold_reason(UnknownEffortLevel("m", "e", accepted=None)))

    @pytest.mark.parametrize(
        "reason",
        [
            None,
            "",
            "PARKED 2026-09-01: waiting on a supplement",
            "DEFERRED: input awaits re-acquisition: /q/papers/PMID_X/PMID_X.pdf",
            readiness.hold_reason(["missing file: /q/a.md"]),
        ],
    )
    def test_rejects_reasons_it_did_not_write(self, reason: str | None) -> None:
        """The supervisor un-parks only its own effort holds, never an
        operator's park, a hook deferral or a readiness hold."""
        assert not is_hold_reason(reason)
