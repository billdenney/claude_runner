"""Tests for ``supervisor.account_pause``, the operator's account pause markers.

The daemon tests pin the contract that matters operationally: a pause or
resume made while the supervisor runs reaches its dispatch step and the
``supervisor.json`` it writes, although the supervisor rewrites that file
from memory every tick. A pause ``supervisor.json`` records when the
markers are first used is kept, and only then: afterwards the file just
echoes the markers, so a resume made while the supervisor is stopped holds.
"""

from __future__ import annotations

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
    assert account_pause.set_paused(tmp_path, "work", paused=True) is True
    assert account_pause.paused_names(tmp_path) == frozenset({"work"})
    assert account_pause.marker_path(tmp_path, "work").read_bytes() == b""
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


@pytest.mark.parametrize("name", ["../escape", ".hidden", "a/b", "", "-dash"])
def test_marker_path_rejects_names_that_are_not_account_names(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="not an account name"):
        account_pause.marker_path(tmp_path, name)
    with pytest.raises(ValueError, match="not an account name"):
        account_pause.set_paused(tmp_path, name, paused=True)
    assert not account_pause.pause_dir(tmp_path).exists()


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
    out = account_pause.refresh(tmp_path, _snapshot(personal=False, work=False))
    assert out.accounts["work"].paused is True
    assert out.accounts["personal"].paused is False


def test_refresh_keeps_the_flags_when_the_markers_cannot_be_read(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    snap = _snapshot(personal=False, work=True)
    _make_unreadable(tmp_path)
    with caplog.at_level(logging.ERROR, logger=account_pause.__name__):
        assert account_pause.refresh(tmp_path, snap) is snap
    assert "cannot read account pause markers" in caplog.text


def test_adopt_snapshot_flags_writes_markers_only_once(tmp_path: Path) -> None:
    snap = _snapshot(personal=True, work=False)
    assert account_pause.adopt_snapshot_flags(tmp_path, snap) == ["personal"]
    assert account_pause.paused_names(tmp_path) == frozenset({"personal"})
    # The operator resumes; the same snapshot must not re-pause the account.
    account_pause.set_paused(tmp_path, "personal", paused=False)
    assert account_pause.adopt_snapshot_flags(tmp_path, snap) == []
    assert account_pause.paused_names(tmp_path) == frozenset()


def test_adopt_snapshot_flags_counts_a_queue_with_nothing_paused_as_adopted(
    tmp_path: Path,
) -> None:
    assert account_pause.adopt_snapshot_flags(tmp_path, _snapshot(work=False)) == []
    assert account_pause.adopt_snapshot_flags(tmp_path, _snapshot(work=True)) == []
    assert account_pause.paused_names(tmp_path) == frozenset()


def test_adopt_snapshot_flags_skips_a_row_that_is_not_an_account_name(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    snap = _snapshot(work=True)
    snap = snap.model_copy(update={"accounts": {**snap.accounts, "../bad": snap.accounts["work"]}})
    with caplog.at_level(logging.WARNING, logger=account_pause.__name__):
        assert account_pause.adopt_snapshot_flags(tmp_path, snap) == ["work"]
    assert "not adopting the pause of '../bad'" in caplog.text


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


def _run_supervisor(
    qd: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    ticks: int,
    between_ticks: dict[int, bool],
    before_dispatch: dict[int, bool] | None = None,
) -> tuple[list[bool], list[bool]]:
    """Run ``ticks`` real supervisor ticks with dispatch stubbed out.

    Returns two lists, one entry per tick: the ``default`` account's
    ``paused`` flag as that tick's dispatch step saw it, and as
    ``supervisor.json`` on disk held it at that moment. ``between_ticks`` maps a tick number to a pause
    (True) or resume (False) made right after that tick's dispatch step,
    the way an operator's ``account pause`` / ``resume`` lands while the
    supervisor runs. ``before_dispatch`` does the same during that tick's
    force-dispatch step, after the tick has written supervisor.json and
    before it dispatches.
    """
    seen: list[bool] = []
    persisted: list[bool] = []
    consumed: list[int] = []

    def record_force_dispatch(**_kw: object) -> None:
        consumed.append(1)
        if before_dispatch and len(consumed) in before_dispatch:
            account_pause.set_paused(qd, "default", paused=before_dispatch[len(consumed)])

    def record_dispatch(**kw: object) -> SupervisorSnapshot:
        snapshot = kw["snapshot"]
        assert isinstance(snapshot, SupervisorSnapshot)
        seen.append(snapshot.accounts["default"].paused)
        persisted.append(_persisted_paused(qd))
        if len(seen) in between_ticks:
            account_pause.set_paused(qd, "default", paused=between_ticks[len(seen)])
        return snapshot

    monkeypatch.setattr(orch_mod, "tick_dispatch", record_dispatch)
    monkeypatch.setattr(fd_mod, "tick_consume", record_force_dispatch)
    monkeypatch.setattr(daemon_mod, "sleep_for_next_poll", lambda **kw: None)
    start_daemon(
        queue_dir=qd,
        settings=load_settings(None),
        source=FakeUsageSource([_reading(T0)] * max(ticks, 1)),
        pending_count_fn=lambda: 0,
        in_flight_count_fn=lambda: 0,
        clock=FakeClock(T0),
        install_signal_handlers=False,
        max_ticks=ticks,
    )
    return seen, persisted


def _persisted_paused(qd: Path) -> bool:
    snap = persist_mod.load(persist_mod.supervisor_state_path(qd))
    assert snap is not None
    return snap.accounts["default"].paused


def test_pause_made_while_the_supervisor_runs_reaches_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, private_global_lock: Path
) -> None:
    qd = _queue(tmp_path)
    seen, persisted = _run_supervisor(qd, monkeypatch, ticks=3, between_ticks={1: True})
    assert seen == [False, True, True]
    assert persisted == [False, True, True]
    assert _persisted_paused(qd) is True


def test_pause_made_just_before_dispatch_holds_that_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, private_global_lock: Path
) -> None:
    """A pause landing after the tick's state write still stops that tick's dispatch."""
    qd = _queue(tmp_path)
    seen, _ = _run_supervisor(qd, monkeypatch, ticks=2, between_ticks={}, before_dispatch={2: True})
    assert seen == [False, True]


def test_resume_made_while_the_supervisor_runs_reaches_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, private_global_lock: Path
) -> None:
    qd = _queue(tmp_path)
    account_pause.set_paused(qd, "default", paused=True)
    seen, persisted = _run_supervisor(qd, monkeypatch, ticks=3, between_ticks={1: False})
    assert seen == [True, False, False]
    assert persisted == [True, False, False]
    assert _persisted_paused(qd) is False


def test_pause_recorded_in_supervisor_json_survives_a_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, private_global_lock: Path
) -> None:
    qd = _queue(tmp_path)
    persist_mod.write_atomic(_snapshot(default=True), persist_mod.supervisor_state_path(qd))
    seen, _ = _run_supervisor(qd, monkeypatch, ticks=2, between_ticks={})
    assert seen == [True, True]
    assert account_pause.paused_names(qd) == frozenset({"default"})
    assert _persisted_paused(qd) is True


def test_resume_made_while_the_supervisor_is_stopped_holds_at_the_next_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, private_global_lock: Path
) -> None:
    """supervisor.json still says paused after the stop; that echo must not
    re-pause the account the operator resumed in between."""
    qd = _queue(tmp_path)
    account_pause.set_paused(qd, "default", paused=True)
    seen, _ = _run_supervisor(qd, monkeypatch, ticks=1, between_ticks={})
    assert seen == [True]
    assert _persisted_paused(qd) is True
    account_pause.set_paused(qd, "default", paused=False)
    seen, persisted = _run_supervisor(qd, monkeypatch, ticks=2, between_ticks={})
    assert seen == [False, False]
    assert persisted == [False, False]
    assert account_pause.paused_names(qd) == frozenset()


def test_marker_present_at_start_reaches_the_first_supervisor_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, private_global_lock: Path
) -> None:
    qd = _queue(tmp_path)
    account_pause.set_paused(qd, "default", paused=True)
    _run_supervisor(qd, monkeypatch, ticks=0, between_ticks={})
    assert _persisted_paused(qd) is True
