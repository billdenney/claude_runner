"""Tests for ``MultiAccountUsageSource``.

Covers:

* Round-robin picker: oldest ``last_capture_at`` wins; cold start
  tie-breaks alphabetically.
* Reading attribution: returned ``UsageReading.account`` matches the
  picked account.
* Exception attribution: inner-source exceptions get the account name
  attached via ``setattr(exc, "account", ...)`` so the daemon can
  route the failure to that account's state.
* Construction: empty source map rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from claude_task_runner.usage.drift import UsageFormatDrift
from claude_task_runner.usage.models import UsageReading, WindowReading
from claude_task_runner.usage.multi_account_source import MultiAccountUsageSource

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


def _reading(util_5h: int, util_7d: int) -> UsageReading:
    return UsageReading(
        captured_at=datetime(2026, 5, 22, tzinfo=UTC),
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
    )


@dataclass
class _FakeAccountState:
    last_capture_at: datetime | None


@dataclass
class _FakeSnapshot:
    accounts: dict[str, _FakeAccountState] = field(default_factory=dict)


class _StubSource:
    """Returns a fixed reading; tracks how many times it was called."""

    def __init__(self, reading: UsageReading) -> None:
        self._reading = reading
        self.calls = 0

    def read(self) -> UsageReading:
        self.calls += 1
        return self._reading


class _RaisingSource:
    """Always raises the given exception."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def read(self) -> UsageReading:
        raise self._exc


# ---------------------------------------------------------------------------
# Constructor
# ---------------------------------------------------------------------------


def test_requires_at_least_one_inner_source() -> None:
    with pytest.raises(ValueError):
        MultiAccountUsageSource(per_account_sources={}, snapshot_getter=lambda: _FakeSnapshot())


# ---------------------------------------------------------------------------
# Round-robin picker
# ---------------------------------------------------------------------------


def test_cold_start_picks_alphabetically_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """All accounts have last_capture_at=None → alphabetical tie-break."""
    sources = {
        "work": _StubSource(_reading(0, 5)),
        "personal": _StubSource(_reading(3, 33)),
    }
    snapshot = _FakeSnapshot(
        accounts={
            "personal": _FakeAccountState(last_capture_at=None),
            "work": _FakeAccountState(last_capture_at=None),
        }
    )
    src = MultiAccountUsageSource(sources, lambda: snapshot)
    reading = src.read()
    assert reading.account == "personal"  # alphabetical
    assert sources["personal"].calls == 1
    assert sources["work"].calls == 0


def test_picks_account_with_oldest_last_capture() -> None:
    sources = {
        "personal": _StubSource(_reading(8, 76)),
        "work": _StubSource(_reading(2, 10)),
    }
    snapshot = _FakeSnapshot(
        accounts={
            "personal": _FakeAccountState(last_capture_at=datetime(2026, 5, 22, 12, 0, tzinfo=UTC)),
            "work": _FakeAccountState(last_capture_at=datetime(2026, 5, 22, 11, 0, tzinfo=UTC)),
        }
    )
    src = MultiAccountUsageSource(sources, lambda: snapshot)
    reading = src.read()
    assert reading.account == "work"  # older capture


def test_picks_never_captured_over_recently_captured() -> None:
    """None last_capture beats any datetime."""
    sources = {
        "personal": _StubSource(_reading(8, 76)),
        "work": _StubSource(_reading(2, 10)),
    }
    snapshot = _FakeSnapshot(
        accounts={
            "personal": _FakeAccountState(last_capture_at=datetime(2026, 5, 22, tzinfo=UTC)),
            "work": _FakeAccountState(last_capture_at=None),
        }
    )
    src = MultiAccountUsageSource(sources, lambda: snapshot)
    reading = src.read()
    assert reading.account == "work"


def test_snapshot_getter_consulted_each_call() -> None:
    """Picker reads the LATEST snapshot, not a stashed copy. Verifies
    that after the daemon updates last_capture_at, the NEXT read picks
    a different account."""
    sources = {
        "personal": _StubSource(_reading(8, 76)),
        "work": _StubSource(_reading(2, 10)),
    }
    state = _FakeSnapshot(
        accounts={
            "personal": _FakeAccountState(last_capture_at=None),
            "work": _FakeAccountState(last_capture_at=None),
        }
    )
    src = MultiAccountUsageSource(sources, lambda: state)

    # First call → personal (alphabetical tie-break).
    r1 = src.read()
    assert r1.account == "personal"
    # Simulate the daemon writing last_capture_at after the read.
    state.accounts["personal"].last_capture_at = datetime(2026, 5, 22, 12, 0, tzinfo=UTC)
    # Second call → work (personal now has a non-None last_capture).
    r2 = src.read()
    assert r2.account == "work"


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


def test_reading_account_set_to_picked_name() -> None:
    sources = {"personal": _StubSource(_reading(1, 2))}
    snapshot = _FakeSnapshot(accounts={"personal": _FakeAccountState(last_capture_at=None)})
    src = MultiAccountUsageSource(sources, lambda: snapshot)
    reading = src.read()
    assert reading.account == "personal"
    assert reading.five_hour.utilization_pct == 1


def test_exception_carries_account_attribute() -> None:
    """Inner exception gets ``.account`` set so the daemon can route it."""
    err = UsageFormatDrift("simulated tui change")
    sources = {"personal": _RaisingSource(err)}
    snapshot = _FakeSnapshot(accounts={"personal": _FakeAccountState(last_capture_at=None)})
    src = MultiAccountUsageSource(sources, lambda: snapshot)
    with pytest.raises(UsageFormatDrift) as exc_info:
        src.read()
    assert getattr(exc_info.value, "account", None) == "personal"


def test_single_account_exception_does_not_break_next_read() -> None:
    """One account raising shouldn't poison the multi-account state."""
    bad = _RaisingSource(UsageFormatDrift("bad"))
    good = _StubSource(_reading(9, 9))
    sources = {"bad": bad, "good": good}
    snapshot = _FakeSnapshot(
        accounts={
            "bad": _FakeAccountState(last_capture_at=None),
            "good": _FakeAccountState(last_capture_at=None),
        }
    )
    src = MultiAccountUsageSource(sources, lambda: snapshot)

    # First call picks 'bad' alphabetically and raises.
    with pytest.raises(UsageFormatDrift):
        src.read()
    # Simulate the daemon recording the failure as a capture (so the
    # next round-robin doesn't loop on bad).
    snapshot.accounts["bad"].last_capture_at = datetime(2026, 5, 22, tzinfo=UTC)
    # Second call goes to 'good'.
    r2 = src.read()
    assert r2.account == "good"
    assert r2.five_hour.utilization_pct == 9
