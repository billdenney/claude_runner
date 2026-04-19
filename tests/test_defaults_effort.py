from __future__ import annotations

from claude_runner.defaults import EFFORT_TABLE, Effort, effort_defaults


def test_all_tiers_defined() -> None:
    assert set(EFFORT_TABLE) == set(Effort)


def test_tiers_monotonic() -> None:
    from itertools import pairwise

    ordered = [Effort.OFF, Effort.LOW, Effort.MEDIUM, Effort.HIGH]
    values = [effort_defaults(e) for e in ordered]
    for a, b in pairwise(values):
        assert a.thinking_budget_tokens <= b.thinking_budget_tokens
        assert a.max_turns <= b.max_turns
        assert a.estimated_input_tokens <= b.estimated_input_tokens


def test_off_disables_thinking() -> None:
    assert effort_defaults(Effort.OFF).thinking_budget_tokens == 0
