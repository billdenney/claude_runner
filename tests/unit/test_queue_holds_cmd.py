"""``claude-task-runner queue holds``: the selector's view, read-only.

The command runs the supervisor's own candidate selection
(``orchestrator.preview_selection``) and reports which tasks would start
and what holds the rest. These tests pin its JSON on a queue with one task
per hold category, the in-flight tasks it takes from supervisor.json, the
warm-up cap, and that it writes nothing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_task_runner.queue.store import queue_runtime_dir, state_path_for, todo_dir
from claude_task_runner.supervisor import persistence as persist_mod
from claude_task_runner.supervisor.states import (
    InFlightRecord,
    SupervisorSnapshot,
    SupervisorState,
)

from ._concurrency_fixtures import invoke_cli
from ._mixed_queue import BLOCK_FILE, add_state, add_task, build_mixed_queue

SINCE = datetime(2026, 10, 16, tzinfo=UTC)

HELD = {
    "block_dispatch": 1,
    "deferred_cooldown": 1,
    "depends_on": 1,
    "failed_circuit_breaker": 1,
    "in_flight": 1,
    "invalid_effort": 1,
    "possibly_hung": 1,
    "readiness_awaiting_sidecar_response": 1,
    "readiness_awaiting_sidecar_response+missing_file": 1,
    "readiness_missing_file": 1,
    "running": 1,
    "unanswered_sidecar": 1,
    "unparseable_state": 1,
    "unparseable_task": 1,
    "weekly_paused": 1,
}


@pytest.fixture
def queue(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    return qd


def _config(queue: Path, extra: str = "") -> None:
    (queue / "claude_runner.toml").write_text(
        f'[dispatch]\ndispatch_block_file = "{BLOCK_FILE}"\n{extra}', encoding="utf-8"
    )


def _in_flight(queue: Path, *task_ids: str) -> None:
    records = [InFlightRecord(task_id=t, account="default", started_at=SINCE) for t in task_ids]
    snapshot = SupervisorSnapshot(
        state=SupervisorState.DISPATCHING,
        since=SINCE,
        in_flight=records,
        in_flight_task_ids=list(task_ids),
    )
    persist_mod.write_atomic(snapshot, persist_mod.supervisor_state_path(queue))


def _holds(queue: Path) -> dict[str, object]:
    result = invoke_cli(["queue", "holds", "--queue", str(queue), "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _states(queue: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in state_path_for(queue, "x").parent.glob("*.yaml")}


def test_every_task_lands_in_one_place(queue: Path, tmp_path: Path) -> None:
    build_mixed_queue(queue, tmp_path / "absent.md")
    _config(queue)
    _in_flight(queue, "inflight")

    assert _holds(queue) == {
        "eligible": {
            "count": 3,
            "task_ids": ["cooldown-over", "ready", "sidecar-answered"],
            "by_status": {"awaiting_sidecar": 1, "deferred": 1, "new": 1},
        },
        "held": HELD,
        "completed": 2,
        "todo_tasks": 20,
        "poll_interval_s": 60.0,
        "queue_cap": None,
        "warming_up": False,
        "supervisor_json_error": None,
    }


def test_writes_no_state_file(queue: Path, tmp_path: Path) -> None:
    """A supervisor tick would park needs-file and bad-effort as deferred;
    the command must leave every state file as it was, and create none."""
    build_mixed_queue(queue, tmp_path / "absent.md")
    _config(queue)
    _in_flight(queue, "inflight")
    before = _states(queue)
    _holds(queue)
    assert _states(queue) == before


def test_without_supervisor_json_nothing_is_in_flight(queue: Path) -> None:
    add_task(queue, "a")
    add_task(queue, "b")
    add_state(queue, "b", "running")
    _config(queue)

    holds = _holds(queue)
    assert (holds["eligible"], holds["held"], holds["supervisor_json_error"]) == (
        {"count": 1, "task_ids": ["a"], "by_status": {"new": 1}},
        {"running": 1},
        None,
    )


def test_unreadable_supervisor_json_is_reported(queue: Path) -> None:
    add_task(queue, "a")
    _config(queue)
    path = persist_mod.supervisor_state_path(queue)
    path.write_text("{not json", encoding="utf-8")

    holds = _holds(queue)
    assert holds["eligible"] == {"count": 1, "task_ids": ["a"], "by_status": {"new": 1}}
    assert holds["supervisor_json_error"] == (
        f"{path}: invalid JSON: Expecting property name enclosed in double quotes: "
        "line 1 column 2 (char 1)"
    )


def test_warm_up_cap_until_a_task_completes(queue: Path) -> None:
    add_task(queue, "a")
    _config(queue, "[concurrency]\ninitial_concurrency = 2\nmax_concurrency = 6\n")
    assert (_holds(queue)["queue_cap"], _holds(queue)["warming_up"]) == (2, True)

    add_task(queue, "b")
    add_state(queue, "b", "completed")
    assert (_holds(queue)["queue_cap"], _holds(queue)["warming_up"]) == (6, False)


def test_poll_interval_comes_from_the_settings(queue: Path) -> None:
    _config(queue, "[usage]\npoll_interval_s = 300\n")
    assert _holds(queue)["poll_interval_s"] == 300.0
