"""Tests for supervisor.persistence — atomic JSON I/O for snapshots."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_task_runner.supervisor.persistence import (
    SupervisorPersistenceError,
    initial_snapshot,
    load,
    supervisor_state_path,
    write_atomic,
)
from claude_task_runner.supervisor.states import SupervisorSnapshot, SupervisorState


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    qd = tmp_path / "queue"
    qd.mkdir()
    return qd


def _snap(state: SupervisorState = SupervisorState.IDLE) -> SupervisorSnapshot:
    return SupervisorSnapshot(
        state=state,
        since=datetime(2026, 5, 4, 12, 0, tzinfo=UTC),
        last_5h_util_pct=42,
        last_weekly_util_pct=5,
    )


class TestPath:
    def test_default_filename(self, queue_dir: Path) -> None:
        p = supervisor_state_path(queue_dir)
        assert p.name == "supervisor.json"
        assert ".claude_task_runner" in str(p)

    def test_custom_filename(self, queue_dir: Path) -> None:
        p = supervisor_state_path(queue_dir, "alt.json")
        assert p.name == "alt.json"


class TestRoundTrip:
    def test_basic(self, queue_dir: Path) -> None:
        path = supervisor_state_path(queue_dir)
        snap = _snap(SupervisorState.DISPATCHING)
        write_atomic(snap, path)
        loaded = load(path)
        assert loaded == snap

    def test_with_optional_fields(self, queue_dir: Path) -> None:
        path = supervisor_state_path(queue_dir)
        snap = SupervisorSnapshot(
            state=SupervisorState.THROTTLED_WEEKLY,
            since=datetime(2026, 5, 4, 12, 0, tzinfo=UTC),
            last_5h_util_pct=20,
            last_weekly_util_pct=92,
            last_5h_reset_at=datetime(2026, 5, 4, 17, 0, tzinfo=UTC),
            last_weekly_reset_at=datetime(2026, 5, 8, 3, 0, tzinfo=UTC),
            in_flight_task_ids=["007-foo", "012-bar"],
            scheduled_wakeup_at=datetime(2026, 5, 4, 23, 0, tzinfo=UTC),
            consecutive_clean_polls=2,
            last_drift_message="prior drift cleared",
        )
        write_atomic(snap, path)
        loaded = load(path)
        assert loaded == snap


class TestAtomicity:
    def test_no_tmp_files_left_behind(self, queue_dir: Path) -> None:
        path = supervisor_state_path(queue_dir)
        write_atomic(_snap(), path)
        leftovers = list(path.parent.glob(".*tmp*"))
        assert leftovers == []

    def test_concurrent_reads_always_complete(self, queue_dir: Path) -> None:
        path = supervisor_state_path(queue_dir)
        write_atomic(_snap(SupervisorState.IDLE), path)
        first = load(path)
        write_atomic(_snap(SupervisorState.DISPATCHING), path)
        second = load(path)
        assert first is not None and first.state is SupervisorState.IDLE
        assert second is not None and second.state is SupervisorState.DISPATCHING


class TestErrors:
    def test_load_missing_returns_none(self, queue_dir: Path) -> None:
        path = supervisor_state_path(queue_dir)
        assert load(path) is None

    def test_load_invalid_json_raises(self, queue_dir: Path) -> None:
        path = supervisor_state_path(queue_dir)
        path.write_text("{not json")
        with pytest.raises(SupervisorPersistenceError, match="invalid JSON"):
            load(path)

    def test_load_non_object_raises(self, queue_dir: Path) -> None:
        path = supervisor_state_path(queue_dir)
        path.write_text("[1, 2, 3]")
        with pytest.raises(SupervisorPersistenceError, match="object"):
            load(path)

    def test_load_unknown_schema_version_raises(self, queue_dir: Path) -> None:
        path = supervisor_state_path(queue_dir)
        path.write_text(
            '{"schema_version": 99, "state": "idle", "since": "2026-05-04T12:00:00+00:00"}'
        )
        with pytest.raises(SupervisorPersistenceError, match="schema_version=99"):
            load(path)


class TestInitialSnapshot:
    def test_starts_in_idle(self) -> None:
        snap = initial_snapshot(since=datetime(2026, 5, 4, 12, 0, tzinfo=UTC))
        assert snap.state is SupervisorState.IDLE
        assert snap.consecutive_clean_polls == 0
        assert snap.in_flight_task_ids == []
        assert snap.last_drift_message == ""


class TestMigrationV3ToV4:
    """ADR-0022: ``paused_weekly`` / ``end_of_week_push`` rewrite to ``idle``,
    ``scheduled_wakeup_at`` clears, in-flight tasks survive."""

    def _write_v3(self, queue_dir: Path, payload: dict) -> Path:
        path = supervisor_state_path(queue_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
        return path

    def test_paused_weekly_rewrites_to_idle(self, queue_dir: Path) -> None:
        v3 = {
            "schema_version": 3,
            "state": "paused_weekly",
            "since": "2026-05-25T12:00:00+00:00",
            "scheduled_wakeup_at": "2026-05-25T18:00:00+00:00",
            "last_5h_util_pct": 80,
            "last_weekly_util_pct": 95,
            "accounts": {
                "default": {
                    "state": "paused_weekly",
                    "since": "2026-05-25T12:00:00+00:00",
                    "scheduled_wakeup_at": "2026-05-25T18:00:00+00:00",
                },
            },
        }
        path = self._write_v3(queue_dir, v3)
        snap = load(path)
        assert snap is not None
        assert snap.state is SupervisorState.IDLE
        assert snap.accounts["default"].state is SupervisorState.IDLE
        assert snap.scheduled_wakeup_at is None
        assert snap.accounts["default"].scheduled_wakeup_at is None

    def test_end_of_week_push_rewrites_to_idle(self, queue_dir: Path) -> None:
        v3 = {
            "schema_version": 3,
            "state": "end_of_week_push",
            "since": "2026-05-25T12:00:00+00:00",
            "accounts": {
                "personal": {
                    "state": "end_of_week_push",
                    "since": "2026-05-25T12:00:00+00:00",
                },
            },
        }
        path = self._write_v3(queue_dir, v3)
        snap = load(path)
        assert snap is not None
        assert snap.state is SupervisorState.IDLE
        assert snap.accounts["personal"].state is SupervisorState.IDLE

    def test_in_flight_tasks_survive(self, queue_dir: Path) -> None:
        v3 = {
            "schema_version": 3,
            "state": "paused_weekly",
            "since": "2026-05-25T12:00:00+00:00",
            "accounts": {
                "a": {"state": "dispatching", "since": "2026-05-25T12:00:00+00:00"},
            },
            "in_flight": [
                {
                    "task_id": "task-001",
                    "account": "a",
                    "started_at": "2026-05-25T11:00:00+00:00",
                },
                {
                    "task_id": "task-002",
                    "account": "a",
                    "started_at": "2026-05-25T11:30:00+00:00",
                },
            ],
            "in_flight_task_ids": ["task-001", "task-002"],
        }
        path = self._write_v3(queue_dir, v3)
        snap = load(path)
        assert snap is not None
        assert len(snap.in_flight) == 2
        assert {r.task_id for r in snap.in_flight} == {"task-001", "task-002"}
        assert snap.in_flight_task_ids == ["task-001", "task-002"]

    def test_non_dropped_states_preserved(self, queue_dir: Path) -> None:
        v3 = {
            "schema_version": 3,
            "state": "dispatching",
            "since": "2026-05-25T12:00:00+00:00",
            "accounts": {
                "a": {"state": "throttled_5h", "since": "2026-05-25T12:00:00+00:00"},
                "b": {"state": "slowing_down", "since": "2026-05-25T12:00:00+00:00"},
            },
        }
        path = self._write_v3(queue_dir, v3)
        snap = load(path)
        assert snap is not None
        assert snap.state is SupervisorState.DISPATCHING
        assert snap.accounts["a"].state is SupervisorState.THROTTLED_5H
        assert snap.accounts["b"].state is SupervisorState.SLOWING_DOWN

    def test_v2_then_v3_then_v4_chain(self, queue_dir: Path) -> None:
        """v2 payload migrates through v3 and v4 to v5 in one load."""
        v2 = {
            "schema_version": 2,
            "state": "idle",
            "since": "2026-05-25T12:00:00+00:00",
        }
        path = self._write_v3(queue_dir, v2)
        snap = load(path)
        assert snap is not None
        assert snap.state is SupervisorState.IDLE
        assert snap.accounts["default"].state is SupervisorState.IDLE


class TestMigrationV4ToV5:
    """``stopped`` rewrites to ``idle``; nothing else in the file changes."""

    def _write(self, queue_dir: Path, payload: dict) -> Path:
        path = supervisor_state_path(queue_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
        return path

    def test_stopped_rewrites_to_idle(self, queue_dir: Path) -> None:
        v4 = {
            "schema_version": 4,
            "state": "stopped",
            "since": "2026-09-25T12:00:00+00:00",
            "accounts": {
                "a": {"state": "stopped", "since": "2026-09-25T12:00:00+00:00"},
                "b": {
                    "state": "throttled_5h",
                    "since": "2026-09-25T12:00:00+00:00",
                    "scheduled_wakeup_at": "2026-09-25T17:05:00+00:00",
                },
            },
            "in_flight": [
                {
                    "task_id": "task-001",
                    "account": "b",
                    "started_at": "2026-09-25T11:00:00+00:00",
                },
            ],
            "in_flight_task_ids": ["task-001"],
        }
        snap = load(self._write(queue_dir, v4))
        assert snap is not None
        assert snap.schema_version == 5
        assert snap.state is SupervisorState.IDLE
        assert snap.accounts["a"].state is SupervisorState.IDLE
        # Other states, their wakeups and in-flight tasks are untouched.
        assert snap.accounts["b"].state is SupervisorState.THROTTLED_5H
        assert snap.accounts["b"].scheduled_wakeup_at == datetime(2026, 9, 25, 17, 5, tzinfo=UTC)
        assert [r.task_id for r in snap.in_flight] == ["task-001"]
        assert snap.in_flight_task_ids == ["task-001"]

    def test_v4_without_stopped_keeps_state_and_wakeup(self, queue_dir: Path) -> None:
        v4 = {
            "schema_version": 4,
            "state": "dispatching",
            "since": "2026-09-25T12:00:00+00:00",
            "scheduled_wakeup_at": "2026-09-25T17:05:00+00:00",
        }
        snap = load(self._write(queue_dir, v4))
        assert snap is not None
        assert snap.schema_version == 5
        assert snap.state is SupervisorState.DISPATCHING
        assert snap.scheduled_wakeup_at == datetime(2026, 9, 25, 17, 5, tzinfo=UTC)

    def test_v3_stopped_migrates_through_to_idle(self, queue_dir: Path) -> None:
        """v3 -> v4 passes ``stopped`` through; v4 -> v5 rewrites it."""
        v3 = {"schema_version": 3, "state": "stopped", "since": "2026-09-25T12:00:00+00:00"}
        snap = load(self._write(queue_dir, v3))
        assert snap is not None
        assert snap.schema_version == 5
        assert snap.state is SupervisorState.IDLE

    def test_malformed_account_entry_still_fails_loud(self, queue_dir: Path) -> None:
        """The migration passes a non-object account through untouched, and
        validation then rejects the file rather than guessing."""
        v4 = {
            "schema_version": 4,
            "state": "stopped",
            "since": "2026-09-25T12:00:00+00:00",
            "accounts": {"a": "not-an-object"},
        }
        with pytest.raises(SupervisorPersistenceError, match="accounts"):
            load(self._write(queue_dir, v4))

    def test_written_snapshot_is_v5(self, queue_dir: Path) -> None:
        path = supervisor_state_path(queue_dir)
        write_atomic(initial_snapshot(since=datetime(2026, 9, 25, 12, 0, tzinfo=UTC)), path)
        assert json.loads(path.read_text())["schema_version"] == 5
