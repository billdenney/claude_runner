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
    def test_starts_in_no_reading(self) -> None:
        """No account takes tasks until a clean reading classifies it."""
        snap = initial_snapshot(since=datetime(2026, 5, 4, 12, 0, tzinfo=UTC))
        assert snap.state is SupervisorState.NO_READING
        assert snap.accounts["default"].state is SupervisorState.NO_READING
        assert snap.accounts["default"].last_reading_at is None
        assert snap.consecutive_clean_polls == 0
        assert snap.in_flight_task_ids == []
        assert snap.last_drift_message == ""


class TestMigrationV3ToV4:
    """ADR-0022: ``paused_weekly`` / ``end_of_week_push`` rewrite to ``idle``,
    ``scheduled_wakeup_at`` clears, in-flight tasks survive.

    A loaded file continues to v7, which turns that ``idle`` into
    ``no_reading``: v3 recorded no reading time.
    """

    def _write_v3(self, queue_dir: Path, payload: dict) -> Path:
        path = supervisor_state_path(queue_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
        return path

    def test_paused_weekly_arrives_as_no_reading(self, queue_dir: Path) -> None:
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
        assert snap.state is SupervisorState.NO_READING
        assert snap.accounts["default"].state is SupervisorState.NO_READING
        assert snap.scheduled_wakeup_at is None
        assert snap.accounts["default"].scheduled_wakeup_at is None

    def test_end_of_week_push_arrives_as_no_reading(self, queue_dir: Path) -> None:
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
        assert snap.state is SupervisorState.NO_READING
        assert snap.accounts["personal"].state is SupervisorState.NO_READING

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
        """States that take no tasks, which v7 keeps too, pass through."""
        v3 = {
            "schema_version": 3,
            "state": "throttled_weekly",
            "since": "2026-05-25T12:00:00+00:00",
            "accounts": {
                "a": {"state": "throttled_5h", "since": "2026-05-25T12:00:00+00:00"},
                "b": {"state": "error_drift", "since": "2026-05-25T12:00:00+00:00"},
            },
        }
        path = self._write_v3(queue_dir, v3)
        snap = load(path)
        assert snap is not None
        assert snap.state is SupervisorState.THROTTLED_WEEKLY
        assert snap.accounts["a"].state is SupervisorState.THROTTLED_5H
        assert snap.accounts["b"].state is SupervisorState.ERROR_DRIFT

    def test_v2_then_v3_then_v4_chain(self, queue_dir: Path) -> None:
        """v2 payload migrates all the way to v7 in one load."""
        v2 = {
            "schema_version": 2,
            "state": "idle",
            "since": "2026-05-25T12:00:00+00:00",
        }
        path = self._write_v3(queue_dir, v2)
        snap = load(path)
        assert snap is not None
        assert snap.schema_version == 7
        assert snap.state is SupervisorState.NO_READING
        assert snap.accounts["default"].state is SupervisorState.NO_READING


class TestMigrationV4ToV5:
    """``stopped`` rewrites to ``idle``; nothing else in the file changes.

    A loaded file continues to v7, where ``idle`` and the other states that
    take tasks become ``no_reading``.
    """

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
        assert snap.schema_version == 7
        assert snap.state is SupervisorState.NO_READING
        assert snap.accounts["a"].state is SupervisorState.NO_READING
        # Other states, their wakeups and in-flight tasks are untouched.
        assert snap.accounts["b"].state is SupervisorState.THROTTLED_5H
        assert snap.accounts["b"].scheduled_wakeup_at == datetime(2026, 9, 25, 17, 5, tzinfo=UTC)
        assert [r.task_id for r in snap.in_flight] == ["task-001"]
        assert snap.in_flight_task_ids == ["task-001"]

    def test_v4_without_stopped_keeps_state_and_wakeup(self, queue_dir: Path) -> None:
        v4 = {
            "schema_version": 4,
            "state": "throttled_5h",
            "since": "2026-09-25T12:00:00+00:00",
            "scheduled_wakeup_at": "2026-09-25T17:05:00+00:00",
        }
        snap = load(self._write(queue_dir, v4))
        assert snap is not None
        assert snap.schema_version == 7
        assert snap.state is SupervisorState.THROTTLED_5H
        assert snap.scheduled_wakeup_at == datetime(2026, 9, 25, 17, 5, tzinfo=UTC)

    def test_v3_stopped_migrates_through_to_no_reading(self, queue_dir: Path) -> None:
        """v3 -> v4 passes ``stopped`` through; v4 -> v5 makes it ``idle``;
        v6 -> v7 makes that ``no_reading``."""
        v3 = {"schema_version": 3, "state": "stopped", "since": "2026-09-25T12:00:00+00:00"}
        snap = load(self._write(queue_dir, v3))
        assert snap is not None
        assert snap.schema_version == 7
        assert snap.state is SupervisorState.NO_READING

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


class TestMigrationV5ToV6:
    """v6 adds ``target_concurrency``; a v5 file loads with it unset."""

    def _write(self, queue_dir: Path, payload: dict) -> Path:
        path = supervisor_state_path(queue_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
        return path

    def test_v5_loads_with_no_target(self, queue_dir: Path) -> None:
        """A state that takes no tasks, so v7 keeps it too."""
        v5 = {
            "schema_version": 5,
            "state": "throttled_5h",
            "since": "2026-09-25T12:00:00+00:00",
            "last_5h_util_pct": 65,
            "scheduled_wakeup_at": "2026-09-25T17:05:00+00:00",
            "accounts": {
                "a": {
                    "state": "throttled_5h",
                    "since": "2026-09-25T12:00:00+00:00",
                    "last_5h_util_pct": 65,
                    "scheduled_wakeup_at": "2026-09-25T17:05:00+00:00",
                },
            },
        }
        snap = load(self._write(queue_dir, v5))
        assert snap is not None
        assert snap.schema_version == 7
        assert snap.target_concurrency is None
        assert snap.accounts["a"].target_concurrency is None
        # Nothing else changes meaning, so nothing else is rewritten.
        assert snap.state is SupervisorState.THROTTLED_5H
        assert snap.accounts["a"].state is SupervisorState.THROTTLED_5H
        assert snap.accounts["a"].last_5h_util_pct == 65
        assert snap.accounts["a"].scheduled_wakeup_at == datetime(2026, 9, 25, 17, 5, tzinfo=UTC)

    def test_target_round_trips(self, queue_dir: Path) -> None:
        path = supervisor_state_path(queue_dir)
        snap = initial_snapshot(
            since=datetime(2026, 9, 25, 12, 0, tzinfo=UTC), account_names=["a"]
        ).model_copy(update={"target_concurrency": 2})
        snap = snap.model_copy(
            update={
                "accounts": {"a": snap.accounts["a"].model_copy(update={"target_concurrency": 2})}
            }
        )
        write_atomic(snap, path)
        loaded = load(path)
        assert loaded is not None
        assert loaded.target_concurrency == 2
        assert loaded.accounts["a"].target_concurrency == 2

    def test_negative_target_fails_loud(self, queue_dir: Path) -> None:
        """A throttled state, which the v7 migration keeps with its target."""
        v6 = {
            "schema_version": 6,
            "state": "throttled_5h",
            "since": "2026-09-25T12:00:00+00:00",
            "accounts": {
                "a": {
                    "state": "throttled_5h",
                    "since": "2026-09-25T12:00:00+00:00",
                    "target_concurrency": -1,
                },
            },
        }
        with pytest.raises(SupervisorPersistenceError, match="target_concurrency"):
            load(self._write(queue_dir, v6))

    def test_written_snapshot_is_v7(self, queue_dir: Path) -> None:
        path = supervisor_state_path(queue_dir)
        write_atomic(initial_snapshot(since=datetime(2026, 9, 25, 12, 0, tzinfo=UTC)), path)
        assert json.loads(path.read_text())["schema_version"] == 7


class TestMigrationV6ToV7:
    """v7 requires a recent clean reading before an account takes tasks.

    A v6 file records no reading time, so every state that takes tasks
    becomes ``no_reading`` with its target and wakeup cleared. States that
    take no tasks, in-flight tasks and ``since`` are kept.
    """

    def _write(self, queue_dir: Path, payload: dict) -> Path:
        path = supervisor_state_path(queue_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
        return path

    @pytest.mark.parametrize(
        ("v6_state", "loaded_state"),
        [
            ("idle", SupervisorState.NO_READING),
            ("dispatching", SupervisorState.NO_READING),
            ("slowing_down", SupervisorState.NO_READING),
            ("throttled_5h", SupervisorState.THROTTLED_5H),
            ("throttled_weekly", SupervisorState.THROTTLED_WEEKLY),
            ("error_drift", SupervisorState.ERROR_DRIFT),
        ],
    )
    def test_each_v6_state(
        self, queue_dir: Path, v6_state: str, loaded_state: SupervisorState
    ) -> None:
        entry = {
            "state": v6_state,
            "since": "2026-09-25T12:00:00+00:00",
            "last_5h_util_pct": 55,
            "scheduled_wakeup_at": "2026-09-25T17:05:00+00:00",
            "target_concurrency": 2,
        }
        v6 = {"schema_version": 6, **entry, "accounts": {"a": dict(entry)}}
        snap = load(self._write(queue_dir, v6))
        assert snap is not None
        assert snap.schema_version == 7
        rewritten = loaded_state is SupervisorState.NO_READING
        for view in (snap, snap.accounts["a"]):
            assert view.state is loaded_state
            assert view.since == datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
            assert view.last_5h_util_pct == 55
            assert view.last_reading_at is None
            assert view.target_concurrency == (None if rewritten else 2)
            assert view.scheduled_wakeup_at == (
                None if rewritten else datetime(2026, 9, 25, 17, 5, tzinfo=UTC)
            )

    def test_covers_every_state_a_v6_file_can_hold(self) -> None:
        """A state added to the enum later needs its own migration rule."""
        covered = {"idle", "dispatching", "slowing_down"} | {
            "throttled_5h",
            "throttled_weekly",
            "error_drift",
        }
        assert {s.value for s in SupervisorState} - covered == {"no_reading"}

    def test_in_flight_tasks_survive(self, queue_dir: Path) -> None:
        v6 = {
            "schema_version": 6,
            "state": "dispatching",
            "since": "2026-09-25T12:00:00+00:00",
            "accounts": {"a": {"state": "dispatching", "since": "2026-09-25T12:00:00+00:00"}},
            "in_flight": [
                {"task_id": "t1", "account": "a", "started_at": "2026-09-25T11:00:00+00:00"},
            ],
            "in_flight_task_ids": ["t1"],
        }
        snap = load(self._write(queue_dir, v6))
        assert snap is not None
        assert [r.task_id for r in snap.in_flight] == ["t1"]
        assert snap.in_flight_task_ids == ["t1"]

    def test_live_v5_file_loads(self, queue_dir: Path) -> None:
        """The shape of the live queue's supervisor.json on 2026-09-26: v5,
        no ``target_concurrency``, two accounts both throttled weekly."""
        acct = {
            "consecutive_clean_polls": 0,
            "last_5h_reset_at": "2026-09-26T21:20:00Z",
            "last_5h_util_pct": 12,
            "last_capture_at": "2026-09-26T20:24:44.904362Z",
            "last_drift_message": "",
            "last_weekly_reset_at": "2026-09-29T18:00:00Z",
            "last_weekly_util_pct": 81,
            "paused": False,
            "scheduled_wakeup_at": "2026-09-26T21:25:00Z",
            "since": "2026-09-26T20:24:44.904348Z",
            "state": "throttled_weekly",
        }
        top = {k: v for k, v in acct.items() if k not in ("last_capture_at", "paused")}
        v5 = {
            "schema_version": 5,
            **top,
            "accounts": {"personal": dict(acct), "work": dict(acct)},
            "in_flight": [],
            "in_flight_task_ids": [],
        }
        snap = load(self._write(queue_dir, v5))
        assert snap is not None
        assert snap.schema_version == 7
        assert snap.state is SupervisorState.THROTTLED_WEEKLY
        for name in ("personal", "work"):
            loaded = snap.accounts[name]
            assert loaded.state is SupervisorState.THROTTLED_WEEKLY
            assert loaded.scheduled_wakeup_at == datetime(2026, 9, 26, 21, 25, tzinfo=UTC)
            assert loaded.target_concurrency is None
            assert loaded.last_reading_at is None
            assert loaded.last_capture_at == datetime(2026, 9, 26, 20, 24, 44, 904362, tzinfo=UTC)

    def test_v5_dispatching_account_arrives_as_no_reading(self, queue_dir: Path) -> None:
        v5 = {
            "schema_version": 5,
            "state": "dispatching",
            "since": "2026-09-25T12:00:00+00:00",
            "accounts": {
                "a": {"state": "dispatching", "since": "2026-09-25T12:00:00+00:00"},
                "b": {"state": "throttled_5h", "since": "2026-09-25T12:00:00+00:00"},
            },
        }
        snap = load(self._write(queue_dir, v5))
        assert snap is not None
        assert snap.state is SupervisorState.NO_READING
        assert snap.accounts["a"].state is SupervisorState.NO_READING
        assert snap.accounts["b"].state is SupervisorState.THROTTLED_5H

    def test_last_reading_at_round_trips(self, queue_dir: Path) -> None:
        path = supervisor_state_path(queue_dir)
        read_at = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
        snap = initial_snapshot(since=read_at, account_names=["a"])
        snap = snap.model_copy(
            update={
                "last_reading_at": read_at,
                "accounts": {
                    "a": snap.accounts["a"].model_copy(update={"last_reading_at": read_at})
                },
            }
        )
        write_atomic(snap, path)
        loaded = load(path)
        assert loaded is not None
        assert loaded.last_reading_at == read_at
        assert loaded.accounts["a"].last_reading_at == read_at
