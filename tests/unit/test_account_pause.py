"""Tests for ``supervisor.account_pause``, the operator's account pause markers.

The daemon tests pin the contract that matters operationally: a pause or
resume made while the supervisor runs reaches its dispatch step and the
``supervisor.json`` it writes on the next tick, although the supervisor
rewrites that file from memory every tick. A pause recorded in
``supervisor.json`` before the markers existed stays in force across a
restart.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from claude_task_runner.clock import FakeClock
from claude_task_runner.config.loader import load_settings
from claude_task_runner.queue.store import queue_runtime_dir, todo_dir
from claude_task_runner.runner import force_dispatch as fd_mod
from claude_task_runner.runner import orchestrator as orch_mod
from claude_task_runner.supervisor import account_pause
from claude_task_runner.supervisor import daemon as daemon_mod
from claude_task_runner.supervisor import persistence as persist_mod
from claude_task_runner.supervisor.daemon import start_daemon
from claude_task_runner.supervisor.states import (
    AccountState,
    SupervisorSnapshot,
    SupervisorState,
)
from claude_task_runner.usage.models import UsageReading, WindowReading
from claude_task_runner.usage.source import FakeUsageSource

T0 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def _snapshot(**paused: bool) -> SupervisorSnapshot:
    """A snapshot with one DISPATCHING account per keyword, paused as given."""
    snap = persist_mod.initial_snapshot(since=T0, account_names=list(paused))
    accounts = {
        name: AccountState(state=SupervisorState.DISPATCHING, since=T0, paused=flag)
        for name, flag in paused.items()
    }
    return snap.model_copy(update={"accounts": accounts})


def _make_unreadable(queue_dir: Path) -> None:
    """Put a regular file where the marker directory belongs."""
    account_pause.pause_dir(queue_dir).parent.mkdir(parents=True, exist_ok=True)
    account_pause.pause_dir(queue_dir).write_text("not a directory", encoding="utf-8")


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------


def test_paused_names_is_empty_when_nothing_was_ever_paused(tmp_path: Path) -> None:
    assert account_pause.paused_names(tmp_path) == frozenset()
    assert not account_pause.pause_dir(tmp_path).exists()


def test_set_paused_creates_and_removes_the_marker(tmp_path: Path) -> None:
    assert account_pause.set_paused(tmp_path, "work", paused=True, now=T0) is True
    assert account_pause.paused_names(tmp_path) == frozenset({"work"})
    marker = account_pause.marker_path(tmp_path, "work")
    assert json.loads(marker.read_text(encoding="utf-8")) == {
        "account": "work",
        "paused_at": T0.isoformat(),
    }
    assert account_pause.set_paused(tmp_path, "work", paused=True) is False
    assert account_pause.set_paused(tmp_path, "work", paused=False) is True
    assert account_pause.paused_names(tmp_path) == frozenset()
    assert account_pause.set_paused(tmp_path, "work", paused=False) is False


def test_paused_names_ignores_temporary_files_and_directories(tmp_path: Path) -> None:
    d = account_pause.pause_dir(tmp_path)
    d.mkdir(parents=True)
    (d / ".work.x1y2.tmp").write_text("{}", encoding="utf-8")
    (d / "subdir").mkdir()
    account_pause.set_paused(tmp_path, "personal", paused=True)
    assert account_pause.paused_names(tmp_path) == frozenset({"personal"})


def test_paused_names_raises_when_the_directory_cannot_be_read(tmp_path: Path) -> None:
    """An unreadable directory must not read as "nothing is paused"."""
    _make_unreadable(tmp_path)
    with pytest.raises(NotADirectoryError):
        account_pause.paused_names(tmp_path)


# ---------------------------------------------------------------------------
# apply / refresh / adopt
# ---------------------------------------------------------------------------


def test_apply_sets_and_clears_flags() -> None:
    out = account_pause.apply(_snapshot(personal=False, work=True), {"personal"})
    assert out.accounts["personal"].paused is True
    assert out.accounts["work"].paused is False
    assert out.accounts["work"].state is SupervisorState.DISPATCHING
    assert out.accounts["work"].since == T0


def test_apply_returns_the_same_snapshot_when_nothing_changes() -> None:
    snap = _snapshot(personal=True, work=False)
    assert account_pause.apply(snap, {"personal"}) is snap


def test_apply_ignores_names_without_an_account_row() -> None:
    snap = _snapshot(work=False)
    assert account_pause.apply(snap, {"ghost"}) is snap


def test_refresh_applies_the_markers(tmp_path: Path) -> None:
    account_pause.set_paused(tmp_path, "work", paused=True)
    out = account_pause.refresh(_snapshot(personal=False, work=False), tmp_path)
    assert out.accounts["work"].paused is True
    assert out.accounts["personal"].paused is False


def test_refresh_keeps_the_flags_when_the_markers_cannot_be_read(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    snap = _snapshot(personal=False, work=True)
    _make_unreadable(tmp_path)
    with caplog.at_level(logging.ERROR, logger=account_pause.__name__):
        assert account_pause.refresh(snap, tmp_path) is snap
    assert "cannot read account pause markers" in caplog.text


def test_adopt_snapshot_flags_writes_markers_for_recorded_pauses(tmp_path: Path) -> None:
    snap = _snapshot(personal=True, work=False)
    assert account_pause.adopt_snapshot_flags(tmp_path, snap) == ["personal"]
    assert account_pause.paused_names(tmp_path) == frozenset({"personal"})
    assert account_pause.adopt_snapshot_flags(tmp_path, snap) == []


# ---------------------------------------------------------------------------
# The supervisor loop
# ---------------------------------------------------------------------------


def _queue(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    return qd


def _reading(captured_at: datetime) -> UsageReading:
    return UsageReading(
        captured_at=captured_at,
        five_hour=WindowReading(
            utilization_pct=10, resets_at_raw="x", resets_at=captured_at + timedelta(hours=5)
        ),
        seven_day=WindowReading(
            utilization_pct=10, resets_at_raw="x", resets_at=captured_at + timedelta(days=7)
        ),
    )


@pytest.fixture
def _isolate_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A per-test global lock, so these tests never contend with a real supervisor."""
    monkeypatch.setattr(
        "claude_task_runner.supervisor.pidfile.global_lock_path",
        lambda: tmp_path / "test_global.lock",
    )


def _run_supervisor(
    qd: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    ticks: int,
    between_ticks: dict[int, bool],
    before_dispatch: dict[int, bool] | None = None,
) -> list[bool]:
    """Run ``ticks`` real supervisor ticks with dispatch stubbed out.

    Returns what each tick's dispatch step saw as the ``default`` account's
    ``paused`` flag. ``between_ticks`` maps a tick number to a pause
    (True) or resume (False) made right after that tick's dispatch step,
    the way an operator's ``account pause`` / ``resume`` lands while the
    supervisor runs. ``before_dispatch`` does the same during that tick's
    force-dispatch step, after the tick has written supervisor.json and
    before it dispatches.
    """
    seen: list[bool] = []
    consumed: list[int] = []

    def record_force_dispatch(**_kw: object) -> None:
        consumed.append(1)
        if before_dispatch and len(consumed) in before_dispatch:
            account_pause.set_paused(qd, "default", paused=before_dispatch[len(consumed)])

    def record_dispatch(**kw: object) -> SupervisorSnapshot:
        snapshot = kw["snapshot"]
        assert isinstance(snapshot, SupervisorSnapshot)
        seen.append(snapshot.accounts["default"].paused)
        if len(seen) in between_ticks:
            account_pause.set_paused(qd, "default", paused=between_ticks[len(seen)])
        return snapshot

    monkeypatch.setattr(orch_mod, "tick_dispatch", record_dispatch)
    monkeypatch.setattr(fd_mod, "tick_consume", record_force_dispatch)
    monkeypatch.setattr(daemon_mod, "sleep_for_next_poll", lambda **kw: None)
    start_daemon(
        queue_dir=qd,
        settings=load_settings(None),
        source=FakeUsageSource([_reading(T0)] * ticks),
        pending_count_fn=lambda: 0,
        in_flight_count_fn=lambda: 0,
        clock=FakeClock(T0),
        install_signal_handlers=False,
        max_ticks=ticks,
    )
    return seen


def _persisted_paused(qd: Path) -> bool:
    snap = persist_mod.load(persist_mod.supervisor_state_path(qd))
    assert snap is not None
    return snap.accounts["default"].paused


def test_pause_made_while_the_supervisor_runs_reaches_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _isolate_lock: None
) -> None:
    qd = _queue(tmp_path)
    seen = _run_supervisor(qd, monkeypatch, ticks=3, between_ticks={1: True})
    assert seen == [False, True, True]
    assert _persisted_paused(qd) is True


def test_pause_made_just_before_dispatch_holds_that_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _isolate_lock: None
) -> None:
    """A pause landing after the tick's state write still stops that tick's dispatch."""
    qd = _queue(tmp_path)
    seen = _run_supervisor(qd, monkeypatch, ticks=2, between_ticks={}, before_dispatch={2: True})
    assert seen == [False, True]


def test_resume_made_while_the_supervisor_runs_reaches_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _isolate_lock: None
) -> None:
    qd = _queue(tmp_path)
    account_pause.set_paused(qd, "default", paused=True)
    seen = _run_supervisor(qd, monkeypatch, ticks=3, between_ticks={1: False})
    assert seen == [True, False, False]
    assert _persisted_paused(qd) is False


def test_pause_recorded_in_supervisor_json_survives_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _isolate_lock: None
) -> None:
    qd = _queue(tmp_path)
    persist_mod.write_atomic(_snapshot(default=True), persist_mod.supervisor_state_path(qd))
    seen = _run_supervisor(qd, monkeypatch, ticks=2, between_ticks={})
    assert seen == [True, True]
    assert account_pause.paused_names(qd) == frozenset({"default"})
    assert _persisted_paused(qd) is True
