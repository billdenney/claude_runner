"""Tests for the daemon's per-account reading attribution.

When a poll result carries ``UsageReading.account = "personal"``,
``run_one_tick`` must:
1. Focus the state machine on ``accounts["personal"]``'s prior state
   (so step() doesn't mix per-account utilization counters).
2. Propagate the new top-level state back into
   ``accounts["personal"]`` after step().
3. Stamp ``accounts["personal"].last_capture_at`` to the current
   clock so the multi-account picker advances on the next tick.

Single-account flows (``reading.account is None``) keep the old
behaviour bit-for-bit.
"""

from __future__ import annotations

from datetime import UTC, datetime

from claude_task_runner.clock import FakeClock
from claude_task_runner.config.loader import load_settings
from claude_task_runner.supervisor.daemon import TickContext, run_one_tick
from claude_task_runner.supervisor.states import (
    AccountState,
    SupervisorSnapshot,
    SupervisorState,
)
from claude_task_runner.usage.models import UsageReading, WindowReading


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
            ),
            "work": AccountState(
                state=SupervisorState.IDLE,
                since=datetime(2026, 5, 22, tzinfo=UTC),
                last_5h_util_pct=0,
                last_weekly_util_pct=0,
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
    """When ``reading.account is None``, the existing single-account
    flow runs untouched: top-level updates, no per-account write."""
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
            "idle": AccountState(state=SupervisorState.IDLE, since=base_since),
            "mid": AccountState(
                state=SupervisorState.DISPATCHING,
                since=base_since,
                last_5h_util_pct=15,
                last_weekly_util_pct=20,
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
                state=SupervisorState.DISPATCHING, since=base_since, last_5h_util_pct=10
            ),
            "b": AccountState(
                state=SupervisorState.DISPATCHING, since=base_since, last_5h_util_pct=10
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
