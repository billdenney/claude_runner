"""Tests for the v2 → v3 supervisor.json migration and v3 schema.

Covers:
* :func:`load` migrates a v2 payload into a v3 SupervisorSnapshot
  with one ``accounts["default"]`` entry.
* v2 ``in_flight_task_ids`` becomes attributed ``InFlightRecord`` in
  ``in_flight``.
* v3 payloads load unchanged.
* :func:`initial_snapshot` seeds ``accounts`` from explicit names.
* Mismatched schema_version still raises.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_task_runner.supervisor.persistence import (
    SupervisorPersistenceError,
    initial_snapshot,
    load,
    write_atomic,
)
from claude_task_runner.supervisor.states import (
    SUPERVISOR_SCHEMA_VERSION,
    AccountState,
    InFlightRecord,
    SupervisorSnapshot,
    SupervisorState,
)


def _v2_payload(in_flight: list[str] | None = None) -> dict[str, object]:
    return {
        "schema_version": 2,
        "state": SupervisorState.SLOWING_DOWN.value,
        "since": "2026-05-15T12:00:00+00:00",
        "last_5h_util_pct": 42,
        "last_weekly_util_pct": 18,
        "last_5h_reset_at": "2026-05-15T17:00:00+00:00",
        "last_weekly_reset_at": None,
        "scheduled_wakeup_at": None,
        "consecutive_clean_polls": 0,
        "last_drift_message": "",
        "in_flight_task_ids": in_flight or [],
    }


class TestV2Migration:
    def test_v2_top_level_fields_become_default_account(self, tmp_path: Path) -> None:
        path = tmp_path / "supervisor.json"
        path.write_text(json.dumps(_v2_payload(in_flight=["t1", "t2"])))
        snap = load(path)
        assert snap is not None
        assert snap.schema_version == SUPERVISOR_SCHEMA_VERSION
        assert "default" in snap.accounts
        acct = snap.accounts["default"]
        assert acct.state == SupervisorState.SLOWING_DOWN
        assert acct.last_5h_util_pct == 42
        assert acct.last_weekly_util_pct == 18

    def test_v2_in_flight_task_ids_become_attributed_records(self, tmp_path: Path) -> None:
        path = tmp_path / "supervisor.json"
        path.write_text(json.dumps(_v2_payload(in_flight=["t1", "t2"])))
        snap = load(path)
        assert snap is not None
        assert len(snap.in_flight) == 2
        for rec in snap.in_flight:
            assert rec.account == "default"

    def test_v2_empty_in_flight_becomes_empty_list(self, tmp_path: Path) -> None:
        path = tmp_path / "supervisor.json"
        payload = _v2_payload(in_flight=[])
        path.write_text(json.dumps(payload))
        snap = load(path)
        assert snap is not None
        assert snap.in_flight == []

    def test_v2_legacy_top_level_fields_still_populated(self, tmp_path: Path) -> None:
        """Legacy state-machine code reads top-level fields; migration must
        preserve them rather than only stuffing into accounts[]."""
        path = tmp_path / "supervisor.json"
        path.write_text(json.dumps(_v2_payload()))
        snap = load(path)
        assert snap is not None
        assert snap.state == SupervisorState.SLOWING_DOWN
        assert snap.last_5h_util_pct == 42


class TestV3LoadsUnchanged:
    def test_v3_native_round_trip(self, tmp_path: Path) -> None:
        snap = SupervisorSnapshot(
            state=SupervisorState.DISPATCHING,
            since=datetime(2026, 5, 21, tzinfo=UTC),
            accounts={
                "personal": AccountState(
                    state=SupervisorState.DISPATCHING,
                    since=datetime(2026, 5, 21, tzinfo=UTC),
                    last_5h_util_pct=15,
                ),
            },
            in_flight=[
                InFlightRecord(
                    task_id="t1",
                    account="personal",
                    started_at=datetime(2026, 5, 21, 1, 0, tzinfo=UTC),
                ),
            ],
        )
        path = tmp_path / "supervisor.json"
        write_atomic(snap, path)
        reloaded = load(path)
        assert reloaded is not None
        assert reloaded.schema_version == SUPERVISOR_SCHEMA_VERSION
        assert reloaded.accounts["personal"].last_5h_util_pct == 15
        assert reloaded.in_flight[0].task_id == "t1"


class TestInitialSnapshot:
    def test_default_seeds_single_default_account(self) -> None:
        snap = initial_snapshot(since=datetime(2026, 5, 21, tzinfo=UTC))
        assert set(snap.accounts) == {"default"}
        assert snap.accounts["default"].state == SupervisorState.IDLE

    def test_multi_account_seed(self) -> None:
        snap = initial_snapshot(
            since=datetime(2026, 5, 21, tzinfo=UTC),
            account_names=["personal", "work"],
        )
        assert set(snap.accounts) == {"personal", "work"}
        for acct in snap.accounts.values():
            assert acct.state == SupervisorState.IDLE


class TestSchemaVersionMismatch:
    def test_unknown_version_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "supervisor.json"
        payload = _v2_payload()
        payload["schema_version"] = 99
        path.write_text(json.dumps(payload))
        with pytest.raises(SupervisorPersistenceError):
            load(path)
