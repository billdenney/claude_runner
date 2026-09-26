"""Tests for the daemon's per-account reading attribution.

When a poll result carries ``UsageReading.account = "personal"``,
``run_one_tick`` must:
1. Focus the state machine on ``accounts["personal"]``'s prior state
   (so step() doesn't mix per-account utilization counters).
2. Propagate the new top-level state back into
   ``accounts["personal"]`` after step().
3. Stamp ``accounts["personal"].last_capture_at`` to the current
   clock so the multi-account picker advances on the next tick.

A single-account queue's source names no account
(``reading.account is None``); its poll results belong to the only
configured account, so they update ``accounts[<that account>]`` too.
Dispatch gates on that per-account state.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from claude_task_runner.clock import FakeClock
from claude_task_runner.config.loader import load_settings
from claude_task_runner.config.schema import (
    AccountConcurrencyPolicy,
    AccountPolicy,
    AccountSettings,
)
from claude_task_runner.supervisor.actions import Notify
from claude_task_runner.supervisor.daemon import TickContext, run_one_tick
from claude_task_runner.supervisor.persistence import initial_snapshot
from claude_task_runner.supervisor.states import (
    AccountState,
    SupervisorSnapshot,
    SupervisorState,
)
from claude_task_runner.usage.drift import UsageFormatDrift
from claude_task_runner.usage.models import UsageReading, WindowReading
from claude_task_runner.usage.multi_account_source import MultiAccountSourceError

READ_AT = datetime(2026, 5, 22, 11, 59, tzinfo=UTC)
"""When the hand-built accounts below were last read: a minute before the
noon ticks, well inside ``[usage].max_reading_age_s``, so only the account
a tick reads changes."""


def _reading(account: str | None, util_5h: int, util_7d: int) -> UsageReading:
    return UsageReading(
        captured_at=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
        five_hour=WindowReading(
            utilization_pct=util_5h,
            resets_at_raw="x",
            resets_at=datetime(2026, 5, 22, 17, tzinfo=UTC),
        ),
        seven_day=WindowReading(
            utilization_pct=util_7d,
            resets_at_raw="x",
            resets_at=datetime(2026, 5, 29, tzinfo=UTC),
        ),
        account=account,
    )


def _snapshot_with_two_accounts() -> SupervisorSnapshot:
    return SupervisorSnapshot(
        state=SupervisorState.IDLE,
        since=datetime(2026, 5, 22, tzinfo=UTC),
        accounts={
            "personal": AccountState(
                state=SupervisorState.IDLE,
                since=datetime(2026, 5, 22, tzinfo=UTC),
                last_5h_util_pct=0,
                last_weekly_util_pct=0,
                last_reading_at=READ_AT,
            ),
            "work": AccountState(
                state=SupervisorState.IDLE,
                since=datetime(2026, 5, 22, tzinfo=UTC),
                last_5h_util_pct=0,
                last_weekly_util_pct=0,
                last_reading_at=READ_AT,
            ),
        },
    )


def test_attributed_reading_updates_correct_account_only() -> None:
    """A reading tagged 'personal' must update accounts['personal'] but
    leave accounts['work'] untouched."""
    settings = load_settings(None)
    clock = FakeClock(start=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC))
    snap = _snapshot_with_two_accounts()

    ctx = TickContext(
        settings=settings,
        poll_result=_reading(account="personal", util_5h=8, util_7d=76),
        pending_count=0,
        in_flight_count=0,
    )
    new_snap, _actions = run_one_tick(snap, ctx, clock)

    # The personal account now reflects the reading.
    assert new_snap.accounts["personal"].last_5h_util_pct == 8
    assert new_snap.accounts["personal"].last_weekly_util_pct == 76
    # Work is untouched.
    assert new_snap.accounts["work"].last_5h_util_pct == 0
    assert new_snap.accounts["work"].last_weekly_util_pct == 0


def test_attributed_reading_stamps_last_capture_at() -> None:
    """The account's last_capture_at must advance to the current clock."""
    settings = load_settings(None)
    clock = FakeClock(start=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC))
    snap = _snapshot_with_two_accounts()
    assert snap.accounts["personal"].last_capture_at is None

    ctx = TickContext(
        settings=settings,
        poll_result=_reading(account="personal", util_5h=8, util_7d=76),
        pending_count=0,
        in_flight_count=0,
    )
    new_snap, _ = run_one_tick(snap, ctx, clock)
    assert new_snap.accounts["personal"].last_capture_at == datetime(
        2026, 5, 22, 12, 0, 0, tzinfo=UTC
    )
    # Work's last_capture_at stays None so the multi-account picker
    # routes the next capture there.
    assert new_snap.accounts["work"].last_capture_at is None


def test_attributed_failure_stamps_last_capture_at() -> None:
    """A failed capture attributed to an account stamps it too.

    The multi-account picker polls the account with the oldest
    last_capture_at, so an account whose captures fail must move to the
    back like one whose capture succeeded, or it would be polled every
    tick while the others went stale.
    """
    settings = load_settings(None).model_copy(
        update={
            "accounts": [
                AccountSettings(name="personal", config_dir=""),
                AccountSettings(name="work", config_dir=""),
            ]
        }
    )
    clock = FakeClock(start=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC))
    failure = MultiAccountSourceError.wrap("work", UsageFormatDrift("simulated drift"))

    new_snap, _ = run_one_tick(
        _snapshot_with_two_accounts(),
        TickContext(settings=settings, poll_result=failure, pending_count=0, in_flight_count=0),
        clock,
    )

    assert new_snap.accounts["work"].state is SupervisorState.ERROR_DRIFT
    assert new_snap.accounts["work"].last_capture_at == datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
    assert new_snap.accounts["personal"].last_capture_at is None


def test_attributed_reading_mirrors_top_level_for_state_machine_backcompat() -> None:
    """After applying an attributed reading, the top-level snapshot
    fields mirror the just-captured account so the existing
    state-machine logic (which reads top-level) still works."""
    settings = load_settings(None)
    clock = FakeClock(start=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC))
    snap = _snapshot_with_two_accounts()

    ctx = TickContext(
        settings=settings,
        poll_result=_reading(account="personal", util_5h=8, util_7d=76),
        pending_count=0,
        in_flight_count=0,
    )
    new_snap, _ = run_one_tick(snap, ctx, clock)
    assert new_snap.last_5h_util_pct == new_snap.accounts["personal"].last_5h_util_pct
    assert new_snap.last_weekly_util_pct == new_snap.accounts["personal"].last_weekly_util_pct


def test_unattributed_reading_keeps_legacy_behavior() -> None:
    """An unnamed reading whose sole configured account (``default``) is
    not in the snapshot updates the top-level fields only: no per-account
    write."""
    settings = load_settings(None)
    clock = FakeClock(start=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC))
    snap = _snapshot_with_two_accounts()
    original_personal_util = snap.accounts["personal"].last_5h_util_pct

    ctx = TickContext(
        settings=settings,
        poll_result=_reading(account=None, util_5h=42, util_7d=55),
        pending_count=0,
        in_flight_count=0,
    )
    new_snap, _ = run_one_tick(snap, ctx, clock)
    # Top-level updated.
    assert new_snap.last_5h_util_pct == 42
    # Per-account NOT updated (because reading wasn't attributed).
    assert new_snap.accounts["personal"].last_5h_util_pct == original_personal_util


def test_attributed_reading_with_unknown_account_falls_back_to_top_level() -> None:
    """Defensive: if an attribution names an account that's not in the
    snapshot, the daemon doesn't crash — it just acts like the reading
    was unattributed and updates the top-level fields."""
    settings = load_settings(None)
    clock = FakeClock(start=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC))
    snap = _snapshot_with_two_accounts()

    ctx = TickContext(
        settings=settings,
        poll_result=_reading(account="ghost", util_5h=11, util_7d=22),
        pending_count=0,
        in_flight_count=0,
    )
    new_snap, _ = run_one_tick(snap, ctx, clock)
    # Top-level reflects the reading; per-account untouched.
    assert new_snap.last_5h_util_pct == 11
    assert new_snap.accounts["personal"].last_5h_util_pct == 0
    assert new_snap.accounts["work"].last_5h_util_pct == 0


def test_round_robin_two_consecutive_attributed_reads() -> None:
    """Sequential reads for two different accounts each update their
    own AccountState slot and bump their own last_capture_at."""
    settings = load_settings(None)
    clock = FakeClock(start=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC))
    snap = _snapshot_with_two_accounts()

    snap, _ = run_one_tick(
        snap,
        TickContext(
            settings=settings,
            poll_result=_reading(account="personal", util_5h=8, util_7d=76),
            pending_count=0,
            in_flight_count=0,
        ),
        clock,
    )
    clock.advance(60)
    snap, _ = run_one_tick(
        snap,
        TickContext(
            settings=settings,
            poll_result=_reading(account="work", util_5h=2, util_7d=10),
            pending_count=0,
            in_flight_count=0,
        ),
        clock,
    )

    assert snap.accounts["personal"].last_5h_util_pct == 8
    assert snap.accounts["work"].last_5h_util_pct == 2
    assert snap.accounts["personal"].last_capture_at == datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
    assert snap.accounts["work"].last_capture_at == datetime(2026, 5, 22, 12, 1, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Per-account isolation across distinct prior states (audit findings 4 & 5)
# ---------------------------------------------------------------------------


def _utc_settings():
    """Settings with ``dispatch_pct.timezone`` pinned to UTC so the
    day/night band selection is deterministic across hosts (otherwise
    the 5h stop threshold flips between the 60% day band and the 90%
    night band depending on the test runner's local time)."""
    base = load_settings(None)
    dp = base.dispatch_pct.model_copy(update={"timezone": "UTC"})
    return base.model_copy(update={"dispatch_pct": dp})


def _reading_5h_only(account: str | None, util_5h: int, util_7d: int) -> UsageReading:
    """A reading that isolates the 5h decision: ``seven_day.resets_at``
    is ``None`` so the weekly trace curve has nothing to anchor to and
    is treated as 'allow dispatch' (matches
    ``test_state_machine::test_weekly_unparseable_falls_back_to_5h``).
    The 5h window carries a reset so THROTTLED_5H can schedule a wakeup."""
    return UsageReading(
        captured_at=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC),
        five_hour=WindowReading(
            utilization_pct=util_5h,
            resets_at_raw="x",
            resets_at=datetime(2026, 5, 22, 17, tzinfo=UTC),
        ),
        seven_day=WindowReading(
            utilization_pct=util_7d,
            resets_at_raw="x",
            resets_at=None,
        ),
        account=account,
    )


def _snapshot_with_three_accounts() -> SupervisorSnapshot:
    """Three accounts seeded in *distinct* prior states so a state
    change is unambiguous: 'idle' IDLE, 'mid' DISPATCHING, 'busy'
    THROTTLED_5H (with a recorded 5h utilization)."""
    base_since = datetime(2026, 5, 22, tzinfo=UTC)
    return SupervisorSnapshot(
        state=SupervisorState.IDLE,
        since=base_since,
        accounts={
            "idle": AccountState(
                state=SupervisorState.IDLE, since=base_since, last_reading_at=READ_AT
            ),
            "mid": AccountState(
                state=SupervisorState.DISPATCHING,
                since=base_since,
                last_5h_util_pct=15,
                last_weekly_util_pct=20,
                last_reading_at=READ_AT,
            ),
            "busy": AccountState(
                state=SupervisorState.THROTTLED_5H,
                since=base_since,
                last_5h_util_pct=65,
                last_weekly_util_pct=30,
            ),
        },
    )


def test_attributed_reading_changes_only_targeted_account_of_three() -> None:
    """Finding 4 — per-account round-trip isolation.

    Three accounts in distinct states; a single reading attributed to
    ``mid`` drives it from DISPATCHING to THROTTLED_5H. The other two
    accounts (``idle``, ``busy``) must be byte-for-byte unchanged — same
    state, utilization, and ``last_capture_at`` (still ``None``)."""
    settings = _utc_settings()
    # Noon UTC → day band (stop=60); 5h=65 throttles ``mid``.
    clock = FakeClock(start=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC))
    snap = _snapshot_with_three_accounts()
    before_idle = snap.accounts["idle"]
    before_busy = snap.accounts["busy"]

    ctx = TickContext(
        settings=settings,
        poll_result=_reading_5h_only(account="mid", util_5h=65, util_7d=20),
        pending_count=3,
        in_flight_count=0,
    )
    new_snap, _ = run_one_tick(snap, ctx, clock)

    # The targeted account moved and recorded the reading.
    assert new_snap.accounts["mid"].state is SupervisorState.THROTTLED_5H
    assert new_snap.accounts["mid"].last_5h_util_pct == 65
    assert new_snap.accounts["mid"].last_capture_at == clock.now()

    # The other two accounts are completely untouched (whole-object eq
    # — catches any stray field mutation, not just `state`).
    assert new_snap.accounts["idle"] == before_idle
    assert new_snap.accounts["busy"] == before_busy
    assert new_snap.accounts["idle"].last_capture_at is None
    assert new_snap.accounts["busy"].last_capture_at is None


def test_alternating_readings_throttle_only_attributed_account_per_tick() -> None:
    """Finding 5 — multi-account isolation under a sequence.

    Two accounts start DISPATCHING. The same throttling-level reading
    (5h=65, day-band stop=60) is fed on alternating ticks, attributed to
    a different account each tick. After each tick only the *attributed*
    account is THROTTLED_5H; the other stays in its prior state until a
    reading is attributed to it."""
    settings = _utc_settings()
    clock = FakeClock(start=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC))
    base_since = datetime(2026, 5, 22, tzinfo=UTC)
    snap = SupervisorSnapshot(
        state=SupervisorState.IDLE,
        since=base_since,
        accounts={
            "a": AccountState(
                state=SupervisorState.DISPATCHING,
                since=base_since,
                last_5h_util_pct=10,
                last_reading_at=READ_AT,
            ),
            "b": AccountState(
                state=SupervisorState.DISPATCHING,
                since=base_since,
                last_5h_util_pct=10,
                last_reading_at=READ_AT,
            ),
        },
    )

    def _tick(account: str) -> None:
        nonlocal snap
        snap, _ = run_one_tick(
            snap,
            TickContext(
                settings=settings,
                poll_result=_reading_5h_only(account=account, util_5h=65, util_7d=20),
                pending_count=3,
                in_flight_count=0,
            ),
            clock,
        )
        clock.advance(60)

    # Tick 1 → attribute to 'a'. Only 'a' throttles.
    _tick("a")
    assert snap.accounts["a"].state is SupervisorState.THROTTLED_5H
    assert snap.accounts["b"].state is SupervisorState.DISPATCHING
    assert snap.accounts["b"].last_5h_util_pct == 10  # 'b' never saw the reading

    # Tick 2 → attribute to 'b'. Now 'b' throttles; 'a' unchanged.
    _tick("b")
    assert snap.accounts["b"].state is SupervisorState.THROTTLED_5H
    assert snap.accounts["a"].state is SupervisorState.THROTTLED_5H
    assert snap.accounts["b"].last_5h_util_pct == 65


# ---------------------------------------------------------------------------
# Single-account queues: unnamed poll results belong to the only account
# ---------------------------------------------------------------------------

_POLICY_MAX_4 = {"default": AccountPolicy(concurrency=AccountConcurrencyPolicy(max_concurrency=4))}
"""The ``default`` account's own policy. Its cap of 4 differs from the
queue-wide default of 2, so a decision that used the queue-wide cap
instead would show up in ``target_concurrency``."""


@pytest.mark.parametrize(
    ("util_5h", "state", "target"),
    [
        (10, SupervisorState.DISPATCHING, 4),
        # Ramp: ceil(4 * (1 - (50 - 40) / (60 - 40))) = 2.
        (50, SupervisorState.SLOWING_DOWN, 2),
        (65, SupervisorState.THROTTLED_5H, 0),
    ],
    ids=["dispatching", "slowing_down", "throttled_5h"],
)
def test_unnamed_reading_updates_the_sole_account(
    util_5h: int, state: SupervisorState, target: int
) -> None:
    clock = FakeClock(start=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC))
    snap = initial_snapshot(since=datetime(2026, 5, 22, tzinfo=UTC), account_names=["default"])

    new_snap, _ = run_one_tick(
        snap,
        TickContext(
            settings=_utc_settings(),  # one account: the legacy "default"
            poll_result=_reading_5h_only(account=None, util_5h=util_5h, util_7d=20),
            pending_count=3,
            in_flight_count=0,
            account_policies=_POLICY_MAX_4,
        ),
        clock,
    )

    acct = new_snap.accounts["default"]
    assert acct.state is state
    assert acct.last_5h_util_pct == util_5h
    assert acct.target_concurrency == target
    assert acct.last_capture_at == datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
    assert new_snap.state is state


def test_unnamed_drift_puts_the_sole_account_in_error_drift() -> None:
    clock = FakeClock(start=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC))
    snap = initial_snapshot(since=datetime(2026, 5, 22, tzinfo=UTC), account_names=["default"])

    new_snap, _ = run_one_tick(
        snap,
        TickContext(
            settings=_utc_settings(),
            poll_result=UsageFormatDrift("only 1 block found"),
            pending_count=3,
            in_flight_count=0,
            account_policies=_POLICY_MAX_4,
        ),
        clock,
    )

    assert new_snap.accounts["default"].state is SupervisorState.ERROR_DRIFT
    assert "only 1 block found" in new_snap.accounts["default"].last_drift_message


# ---------------------------------------------------------------------------
# target_concurrency moves between the account and the top-level view
# ---------------------------------------------------------------------------

_POLICIES_5_AND_1 = {
    "personal": AccountPolicy(concurrency=AccountConcurrencyPolicy(max_concurrency=5)),
    "work": AccountPolicy(concurrency=AccountConcurrencyPolicy(max_concurrency=1)),
}


def _two_accounts(personal_target: int) -> SupervisorSnapshot:
    """``personal`` is slowing down at ``personal_target``. The top-level
    view mirrors ``work``, captured last, whose target is 1."""
    since = datetime(2026, 5, 22, tzinfo=UTC)
    return SupervisorSnapshot(
        state=SupervisorState.DISPATCHING,
        since=since,
        last_5h_util_pct=10,
        target_concurrency=1,
        accounts={
            "personal": AccountState(
                state=SupervisorState.SLOWING_DOWN,
                since=since,
                last_5h_util_pct=55,
                target_concurrency=personal_target,
                last_reading_at=READ_AT,
            ),
            "work": AccountState(
                state=SupervisorState.DISPATCHING,
                since=since,
                last_5h_util_pct=10,
                target_concurrency=1,
                last_reading_at=READ_AT,
            ),
        },
    )


def _tick_personal(snap: SupervisorSnapshot, util_5h: int) -> tuple[SupervisorSnapshot, list]:
    clock = FakeClock(start=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC))
    return run_one_tick(
        snap,
        TickContext(
            settings=_utc_settings(),
            poll_result=_reading_5h_only(account="personal", util_5h=util_5h, util_7d=20),
            pending_count=3,
            in_flight_count=0,
            account_policies=_POLICIES_5_AND_1,
        ),
        clock,
    )


def test_new_target_lands_on_the_account() -> None:
    """personal climbs from 45% (target 4) to 55%: its target becomes 2,
    and work's stays 1."""
    new_snap, actions = _tick_personal(_two_accounts(personal_target=4), util_5h=55)

    assert new_snap.accounts["personal"].target_concurrency == 2
    assert new_snap.accounts["work"].target_concurrency == 1
    assert [a.message for a in actions if isinstance(a, Notify)] == [
        "slowing dispatch: 5h=55% in [40, 60) (day); target concurrency=2/5"
    ]


def test_previous_target_comes_from_the_account() -> None:
    """personal's target is unchanged at 2, so there is no new notice,
    even though the top-level view held work's target of 1."""
    new_snap, actions = _tick_personal(_two_accounts(personal_target=2), util_5h=55)

    assert new_snap.accounts["personal"].target_concurrency == 2
    assert not any(isinstance(a, Notify) for a in actions)


def test_unnamed_reading_with_several_accounts_updates_only_the_top_level() -> None:
    """With two accounts configured an unnamed reading cannot be placed:
    it updates the top-level view alone, decided against the queue-wide
    ``[concurrency].max_concurrency`` (2 by default)."""
    base = _utc_settings()
    settings = base.model_copy(
        update={
            "accounts": [
                AccountSettings(name="personal", config_dir=""),
                AccountSettings(name="work", config_dir=""),
            ]
        }
    )
    clock = FakeClock(start=datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC))
    snap = _snapshot_with_two_accounts()

    new_snap, _ = run_one_tick(
        snap,
        TickContext(
            settings=settings,
            poll_result=_reading_5h_only(account=None, util_5h=10, util_7d=20),
            pending_count=3,
            in_flight_count=0,
            account_policies=_POLICIES_5_AND_1,
        ),
        clock,
    )

    assert new_snap.state is SupervisorState.DISPATCHING
    assert new_snap.target_concurrency == 2
    assert new_snap.accounts == snap.accounts
