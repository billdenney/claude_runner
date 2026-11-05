"""The dispatcher's writes to a running task's state while its worker runs.

``runner.dispatcher._write_running`` writes the heartbeat, the monitor's
tick and the session from the dispatch's own copy of the state, which says
``running``. Once another writer, such as a reaper, has moved the task off
``running``, the write stands down, so that record is not written back. The
dispatch and adoption paths' use of it are tested with them
(``test_dispatcher_file_backed.py``, ``test_dispatcher_adopt.py``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_task_runner.queue.schema import TaskState
from claude_task_runner.queue.store import load_state, write_state_atomic
from claude_task_runner.runner.dispatcher import (
    _write_running,
    _write_unless_finalized,
    park_live_worker,
)

_PID = 4242
_AT = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
_LOGGER = "claude_task_runner.runner.dispatcher"


def _running(task_id: str) -> TaskState:
    return TaskState(
        task_id=task_id,
        status="running",
        attempts=1,
        last_started_at=_AT,
        pid=_PID,
        log_path=f"/q/logs/{task_id}/attempt-1.stream.jsonl",
    )


def _failed(state: TaskState) -> TaskState:
    """The stuck-sleep-loop reaper's record of a worker it killed."""
    return state.model_copy(
        update={
            "status": "failed",
            "stop_reason": "killed_stuck_sleep_loop",
            "pid": None,
            "log_path": None,
        }
    )


def _parked(state: TaskState) -> TaskState:
    return park_live_worker(state, why="but showed no sign of life")


def _stand_downs(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == _LOGGER]


def test_a_running_state_is_written(tmp_path: Path) -> None:
    path = tmp_path / "state.yaml"
    state = _running("t-writes-running")
    write_state_atomic(state, path)

    got = _write_running(state, path, {"last_heartbeat_at": _AT})

    assert got == state.model_copy(update={"last_heartbeat_at": _AT})
    assert load_state(path) == got


@pytest.mark.parametrize(
    ("record", "status"), [(_parked, "possibly_hung"), (_failed, "failed")], ids=["park", "kill"]
)
def test_a_record_another_writer_made_stands(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    record: Callable[[TaskState], TaskState],
    status: str,
) -> None:
    """A reaper parked the task, its worker still running, or killed it.
    The running copy is not written back over either record, and the
    stand-down is logged once, though the writes repeat."""
    path = tmp_path / "state.yaml"
    state = _running(f"t-writes-{status}")
    other = record(state)
    write_state_atomic(other, path)

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        got = _write_running(state, path, {"dispatcher_alive_at": _AT})
        again = _write_running(state, path, {"last_heartbeat_at": _AT})

    assert got == again == state
    assert load_state(path) == other
    assert _stand_downs(caplog) == [
        f"task t-writes-{status}: status changed to {status}; standing down so the "
        "concurrent writer's record wins"
    ]


def test_each_status_another_writer_records_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "state.yaml"
    state = _running("t-writes-both")

    with caplog.at_level(logging.INFO, logger=_LOGGER):
        write_state_atomic(_parked(state), path)
        _write_running(state, path, {"last_heartbeat_at": _AT})
        write_state_atomic(_failed(state), path)
        _write_running(state, path, {"last_heartbeat_at": _AT})

    assert [m.split(";")[0] for m in _stand_downs(caplog)] == [
        "task t-writes-both: status changed to possibly_hung",
        "task t-writes-both: status changed to failed",
    ]


def test_a_park_on_the_same_worker_stands_only_for_the_running_writes(tmp_path: Path) -> None:
    """The run's finalize, which names its worker, may replace a park on
    that worker; a running-state write, which names none, may not."""
    path = tmp_path / "state.yaml"
    state = _running("t-writes-finalize")
    parked = _parked(state)
    final = state.model_copy(update={"status": "completed", "pid": None, "log_path": None})

    write_state_atomic(parked, path)
    assert _write_unless_finalized(final, path, worker_pid=None) == (parked, False)
    assert _write_unless_finalized(final, path, worker_pid=_PID + 1) == (parked, False)
    assert _write_unless_finalized(final, path, worker_pid=_PID) == (final, True)
    assert load_state(path) == final


def test_a_state_file_that_cannot_be_read_is_written_over(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """With nothing on record to stand down for, the write restores a
    readable state."""
    path = tmp_path / "state.yaml"
    path.write_text("status: [unclosed\n")
    state = _running("t-writes-unreadable")

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        got = _write_running(state, path, {"last_heartbeat_at": _AT})

    assert load_state(path) == got == state.model_copy(update={"last_heartbeat_at": _AT})
    [message] = _stand_downs(caplog)
    assert message.startswith(f"task t-writes-unreadable: recheck-load of {path} failed (")
    assert message.endswith("); persisting anyway")
