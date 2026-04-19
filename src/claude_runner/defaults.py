"""Effort-tier → concrete parameter mapping and other hard-coded defaults."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Effort(str, Enum):
    OFF = "off"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class EffortDefaults:
    thinking_budget_tokens: int
    max_turns: int
    estimated_input_tokens: int


EFFORT_TABLE: dict[Effort, EffortDefaults] = {
    Effort.OFF: EffortDefaults(
        thinking_budget_tokens=0, max_turns=10, estimated_input_tokens=8_000
    ),
    Effort.LOW: EffortDefaults(
        thinking_budget_tokens=2_000, max_turns=20, estimated_input_tokens=25_000
    ),
    Effort.MEDIUM: EffortDefaults(
        thinking_budget_tokens=8_000, max_turns=40, estimated_input_tokens=80_000
    ),
    Effort.HIGH: EffortDefaults(
        thinking_budget_tokens=24_000, max_turns=80, estimated_input_tokens=250_000
    ),
}


def effort_defaults(effort: Effort) -> EffortDefaults:
    return EFFORT_TABLE[effort]


# Plan-level budget presets. Numbers are best-effort educated guesses — the
# exact Pro/Max quotas are not publicly disclosed, so the controller always
# reconciles these against observed `ccusage` output when available.
@dataclass(frozen=True, slots=True)
class PlanPreset:
    budget_5h_tokens: int
    budget_weekly_tokens: int


PLAN_PRESETS: dict[str, PlanPreset] = {
    "pro": PlanPreset(budget_5h_tokens=400_000, budget_weekly_tokens=6_000_000),
    "max5": PlanPreset(budget_5h_tokens=2_000_000, budget_weekly_tokens=30_000_000),
    "max20": PlanPreset(budget_5h_tokens=8_000_000, budget_weekly_tokens=120_000_000),
    "team": PlanPreset(budget_5h_tokens=4_000_000, budget_weekly_tokens=60_000_000),
    "custom": PlanPreset(budget_5h_tokens=1_000_000, budget_weekly_tokens=15_000_000),
}

DEFAULT_ALLOWED_TOOLS: tuple[str, ...] = ("Read", "Edit", "Bash", "Grep", "Glob")
DEFAULT_MODEL = "claude-opus-4-7"
DEFAULT_EFFORT = Effort.MEDIUM
DEFAULT_PRIORITY = "normal"
DEFAULT_DISCOVERY_CACHE_TTL_S = 60
