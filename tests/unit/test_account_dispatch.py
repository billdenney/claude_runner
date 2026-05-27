"""Tests for the pure ``choose_account`` dispatch policy.

Drives the policy with hand-built ``AccountState`` / ``ResolvedAccount`` /
``InFlightRecord`` inputs to exercise every branch: pinned to known
account, pinned to unknown account, pinned-but-paused, pinned-but-
throttled, unpinned with multiple candidates (util tie-breaks),
pinned-at-capacity, unpinned with no eligible account.

All accounts are equal priority — the dispatcher picks the least-
utilized account with free capacity. There is no queue-wide cap;
``max_concurrency`` comes from each account's
``<config_dir>/runner-account.toml`` via :class:`ResolvedAccount`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from claude_task_runner.config.schema import (
    AccountConcurrencyPolicy,
    AccountPolicy,
    ResolvedAccount,
)
from claude_task_runner.queue.schema import Task
from claude_task_runner.runner.account_dispatch import (
    account_in_flight_count,
    choose_account,
)
from claude_task_runner.supervisor.states import (
    AccountState,
    InFlightRecord,
    SupervisorState,
)


def _state(
    *,
    state: SupervisorState = SupervisorState.DISPATCHING,
    util_5h: int = 0,
    util_weekly: int = 0,
    paused: bool = False,
) -> AccountState:
    return AccountState(
        state=state,
        since=datetime(2026, 5, 21, tzinfo=UTC),
        last_5h_util_pct=util_5h,
        last_weekly_util_pct=util_weekly,
        paused=paused,
    )


def _account(name: str, *, cap: int = 5) -> ResolvedAccount:
    """Resolved account with the given per-account concurrency cap."""
    return ResolvedAccount(
        name=name,
        config_dir="",
        policy=AccountPolicy(concurrency=AccountConcurrencyPolicy(max_concurrency=cap)),
    )


def _task(task_id: str = "t1", account: str | None = None) -> Task:
    return Task(id=task_id, title="t", prompt="x", account=account)


def _in_flight(account: str, n: int) -> list[InFlightRecord]:
    return [
        InFlightRecord(
            task_id=f"{account}-{i}",
            account=account,
            started_at=datetime(2026, 5, 21, tzinfo=UTC),
        )
        for i in range(n)
    ]


class TestPinned:
    def test_pinned_to_known_eligible(self) -> None:
        choice = choose_account(
            task=_task(account="personal"),
            accounts={"personal": _account("personal")},
            account_states={"personal": _state()},
            in_flight=[],
        )
        assert choice.account == "personal"
        assert "pinned" in choice.reason

    def test_pinned_to_unknown_account(self) -> None:
        choice = choose_account(
            task=_task(account="ghost"),
            accounts={"personal": _account("personal")},
            account_states={"personal": _state()},
            in_flight=[],
        )
        assert choice.account is None
        assert "unknown" in choice.reason

    def test_pinned_to_account_without_state(self) -> None:
        """Configured account but no AccountState yet (cold start)."""
        choice = choose_account(
            task=_task(account="personal"),
            accounts={"personal": _account("personal")},
            account_states={},
            in_flight=[],
        )
        assert choice.account is None
        assert "no state" in choice.reason

    def test_pinned_paused(self) -> None:
        choice = choose_account(
            task=_task(account="personal"),
            accounts={"personal": _account("personal")},
            account_states={"personal": _state(paused=True)},
            in_flight=[],
        )
        assert choice.account is None
        assert "paused" in choice.reason

    def test_pinned_throttled_5h(self) -> None:
        choice = choose_account(
            task=_task(account="personal"),
            accounts={"personal": _account("personal")},
            account_states={"personal": _state(state=SupervisorState.THROTTLED_5H)},
            in_flight=[],
        )
        assert choice.account is None
        assert "throttled_5h" in choice.reason

    def test_pinned_at_per_account_cap(self) -> None:
        choice = choose_account(
            task=_task(account="personal"),
            accounts={"personal": _account("personal", cap=2)},
            account_states={"personal": _state()},
            in_flight=_in_flight("personal", 2),
        )
        assert choice.account is None
        assert "capacity" in choice.reason


class TestUnpinnedPolicy:
    def test_single_eligible_account(self) -> None:
        choice = choose_account(
            task=_task(),
            accounts={"personal": _account("personal")},
            account_states={"personal": _state()},
            in_flight=[],
        )
        assert choice.account == "personal"

    def test_least_utilized_wins(self) -> None:
        """Equal-priority dispatch picks the lowest 5h util account."""
        choice = choose_account(
            task=_task(),
            accounts={
                "personal": _account("personal"),
                "work": _account("work"),
            },
            account_states={
                "personal": _state(util_5h=50),
                "work": _state(util_5h=10),
            },
            in_flight=[],
        )
        assert choice.account == "work"
        assert "5h_util=10%" in choice.reason

    def test_tiebreak_by_weekly_then_name(self) -> None:
        """Same 5h util → lower weekly util → lexicographic name."""
        choice = choose_account(
            task=_task(),
            accounts={
                "b": _account("b"),
                "a": _account("a"),
            },
            account_states={
                "a": _state(util_5h=0, util_weekly=0),
                "b": _state(util_5h=0, util_weekly=0),
            },
            in_flight=[],
        )
        assert choice.account == "a"

    def test_tiebreak_weekly_picks_lower(self) -> None:
        """Same 5h util but different weekly util → lower weekly wins."""
        choice = choose_account(
            task=_task(),
            accounts={
                "a": _account("a"),
                "b": _account("b"),
            },
            account_states={
                "a": _state(util_5h=20, util_weekly=80),
                "b": _state(util_5h=20, util_weekly=30),
            },
            in_flight=[],
        )
        assert choice.account == "b"

    def test_skip_paused_account(self) -> None:
        choice = choose_account(
            task=_task(),
            accounts={
                "personal": _account("personal"),
                "work": _account("work"),
            },
            account_states={
                "personal": _state(paused=True),
                "work": _state(),
            },
            in_flight=[],
        )
        assert choice.account == "work"

    def test_skip_throttled_account(self) -> None:
        choice = choose_account(
            task=_task(),
            accounts={
                "personal": _account("personal"),
                "work": _account("work"),
            },
            account_states={
                "personal": _state(state=SupervisorState.THROTTLED_WEEKLY),
                "work": _state(),
            },
            in_flight=[],
        )
        assert choice.account == "work"

    def test_skip_at_capacity_account(self) -> None:
        choice = choose_account(
            task=_task(),
            accounts={
                "personal": _account("personal", cap=2),
                "work": _account("work"),
            },
            account_states={
                "personal": _state(),
                "work": _state(),
            },
            in_flight=_in_flight("personal", 2),
        )
        assert choice.account == "work"

    def test_no_eligible_returns_none(self) -> None:
        choice = choose_account(
            task=_task(),
            accounts={
                "personal": _account("personal"),
                "work": _account("work"),
            },
            account_states={
                "personal": _state(state=SupervisorState.THROTTLED_WEEKLY),
                "work": _state(paused=True),
            },
            in_flight=[],
        )
        assert choice.account is None
        assert "no eligible" in choice.reason

    def test_per_account_cap_enforced(self) -> None:
        """Per-account max_concurrency from runner-account.toml is the only cap."""
        choice = choose_account(
            task=_task(),
            accounts={"personal": _account("personal", cap=3)},
            account_states={"personal": _state()},
            in_flight=_in_flight("personal", 3),
        )
        assert choice.account is None
        assert "capacity" in choice.reason

    def test_idle_account_is_dispatchable(self) -> None:
        """Cold-start IDLE accounts must be eligible — otherwise the first
        tick never dispatches and the supervisor stalls waiting for
        /usage to land."""
        choice = choose_account(
            task=_task(),
            accounts={"personal": _account("personal")},
            account_states={"personal": _state(state=SupervisorState.IDLE)},
            in_flight=[],
        )
        assert choice.account == "personal"


class TestInFlightCount:
    @pytest.mark.parametrize(
        "account, expected",
        [("personal", 3), ("work", 2), ("other", 0)],
    )
    def test_count_by_account(self, account: str, expected: int) -> None:
        in_flight = _in_flight("personal", 3) + _in_flight("work", 2)
        assert account_in_flight_count(account, in_flight) == expected
