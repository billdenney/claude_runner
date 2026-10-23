"""The selector's missing-dependency gate (ADR-0036).

A ``depends_on`` id that names a task the queue has, a ``todo/`` YAML or a
state file, holds the task until that task completes: ``depends_on``. An id
that names no task can never complete, so the selector holds the task as
``depends_on_missing`` instead, parks it ``deferred`` with a ``missing
dependency:`` reason and one WARNING per transition, and un-parks it the
first tick after its ``depends_on`` is fixed or the missing task is created.
A read-only selection (``queue holds``) reports the same hold and writes and
logs nothing.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import pytest

from claude_task_runner.config.loader import load_defaults, load_settings
from claude_task_runner.queue.schema import TaskState
from claude_task_runner.queue.store import (
    load_state,
    queue_runtime_dir,
    state_file_path,
    state_path_for,
    task_path_for,
    todo_dir,
)
from claude_task_runner.runner import orchestrator as orchestrator_mod
from claude_task_runner.runner.orchestrator import _eligible_candidates, _state_task_ids

from ._mixed_queue import add_state, add_task, forbid_selector_writes

DEFAULT_LEVELS: dict[str, list[str]] = load_defaults()["effort_levels"]
ORCHESTRATOR_LOGGER = "claude_task_runner.runner.orchestrator"
NOW = dt.datetime(2026, 10, 23, 12, 0, 0, tzinfo=dt.UTC)

GHOST_REASON = (
    "missing dependency: depends_on names ghost, but no task in todo/ or state/ has that id"
)
FIX_HINT = ". Fix the task's depends_on, or create the missing task in todo/"


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    return qd


def _select(
    qd: Path, *, read_only: bool = False
) -> tuple[list[str], Counter[str], dict[str, list[str]]]:
    """One selector pass as a supervisor tick makes it: the completed and
    recorded ids from one pass over the state files."""
    ids = _state_task_ids(qd)
    skips: Counter[str] = Counter()
    missing: dict[str, list[str]] = {}
    eligible = _eligible_candidates(
        qd,
        {},
        ids.completed,
        now=NOW,
        effort_levels=DEFAULT_LEVELS,
        skips=skips,
        read_only=read_only,
        recorded_ids=ids.recorded,
        missing_dependencies=missing,
    )
    return sorted(t.id for t in eligible), skips, missing


def _state(qd: Path, task_id: str) -> TaskState:
    return load_state(state_path_for(qd, task_id))


def _messages(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == ORCHESTRATOR_LOGGER and r.levelno == level
    ]


def _warning(task_id: str, reason: str) -> str:
    return f"parking task {task_id} as deferred: {reason}{FIX_HINT}"


# ---------------------------------------------------------------------------
# Which category
# ---------------------------------------------------------------------------


def test_missing_pending_and_completed_dependencies(queue_dir: Path) -> None:
    """Missing, unfinished and completed dependencies: one each."""
    add_task(queue_dir, "upstream")
    add_task(queue_dir, "finished")
    add_state(queue_dir, "finished", "completed")
    add_task(queue_dir, "waits-missing", depends_on=["ghost"])
    add_task(queue_dir, "waits-pending", depends_on=["upstream"])
    add_task(queue_dir, "waits-done", depends_on=["finished"])

    eligible, skips, missing = _select(queue_dir)

    assert eligible == ["upstream", "waits-done"]
    assert skips == Counter({"depends_on_missing": 1, "depends_on": 1, "completed": 1})
    assert missing == {"waits-missing": ["ghost"]}


def _todo_yaml(qd: Path, task_id: str) -> None:
    add_task(qd, task_id)


def _state_only(qd: Path, task_id: str) -> None:
    add_state(qd, task_id, "failed_circuit_breaker")


def _unparseable_todo_yaml(qd: Path, task_id: str) -> None:
    (todo_dir(qd) / f"{task_id}.yaml").write_text("not: valid: yaml: [\n", encoding="utf-8")


def _unparseable_state(qd: Path, task_id: str) -> None:
    state_path_for(qd, task_id).write_text("not yaml: ][[", encoding="utf-8")


@pytest.mark.parametrize(
    ("make_dependency", "expected_eligible", "expected_skips"),
    [
        (_todo_yaml, ["upstream"], Counter({"depends_on": 1})),
        (_state_only, [], Counter({"depends_on": 1})),
        (_unparseable_todo_yaml, [], Counter({"depends_on": 1, "unparseable_task": 1})),
        (_unparseable_state, [], Counter({"depends_on": 1})),
    ],
    ids=["todo-yaml", "state-file-only", "unparseable-todo-yaml", "unparseable-state-file"],
)
def test_a_dependency_with_any_file_exists(
    queue_dir: Path,
    caplog: pytest.LogCaptureFixture,
    make_dependency: Callable[[Path, str], None],
    expected_eligible: list[str],
    expected_skips: Counter[str],
) -> None:
    """A todo/ YAML or a state file, readable or not, is a task that exists:
    the waiting task waits for it, unparked and without a WARNING."""
    make_dependency(queue_dir, "upstream")
    add_task(queue_dir, "waits", depends_on=["upstream"])

    with caplog.at_level(logging.WARNING, logger=ORCHESTRATOR_LOGGER):
        eligible, skips, missing = _select(queue_dir)

    assert (eligible, skips, missing) == (expected_eligible, expected_skips, {})
    assert not state_file_path(queue_dir, "waits").exists()
    assert [m for m in _messages(caplog, logging.WARNING) if "waits" in m] == []


def test_a_missing_dependency_wins_over_an_unfinished_one(
    queue_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Waiting cannot clear a missing dependency, so it is the hold
    reported. The missing ids are listed once each, in depends_on order."""
    add_task(queue_dir, "upstream")
    add_task(queue_dir, "t", depends_on=["upstream", "ghost-b", "ghost-a", "ghost-b"])
    reason = (
        "missing dependency: depends_on names ghost-b, ghost-a, but no task in todo/ or "
        "state/ has those ids"
    )

    with caplog.at_level(logging.WARNING, logger=ORCHESTRATOR_LOGGER):
        eligible, skips, missing = _select(queue_dir)

    assert eligible == ["upstream"]
    assert skips == Counter({"depends_on_missing": 1})
    assert missing == {"t": ["ghost-b", "ghost-a"]}
    assert _messages(caplog, logging.WARNING) == [_warning("t", reason)]
    assert _state(queue_dir, "t").deferred_reason == reason


def test_a_completed_task_is_never_parked_for_a_missing_dependency(
    queue_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    add_task(queue_dir, "done", depends_on=["ghost"])
    add_state(queue_dir, "done", "completed")
    before = state_path_for(queue_dir, "done").read_bytes()

    with caplog.at_level(logging.WARNING, logger=ORCHESTRATOR_LOGGER):
        _eligible, skips, missing = _select(queue_dir)

    assert (skips, missing) == (Counter({"completed": 1}), {})
    assert _messages(caplog, logging.WARNING) == []
    assert state_path_for(queue_dir, "done").read_bytes() == before


def test_given_the_recorded_ids_the_selector_lists_no_state_directory(
    queue_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tick passes the ids its one pass over the state files found; the
    selector takes those as the state files that exist, without a scan of
    its own."""
    add_task(queue_dir, "t", depends_on=["recorded-elsewhere"])

    def no_scan(_queue_dir: Path) -> None:
        raise AssertionError("the selector listed the state directory")

    monkeypatch.setattr(orchestrator_mod, "iter_state_files", no_scan)
    skips: Counter[str] = Counter()
    _eligible_candidates(
        queue_dir,
        {},
        set(),
        now=NOW,
        effort_levels=DEFAULT_LEVELS,
        skips=skips,
        read_only=True,
        recorded_ids={"recorded-elsewhere"},
    )

    assert skips == Counter({"depends_on": 1})


# ---------------------------------------------------------------------------
# Parking, the WARNING, and un-parking
# ---------------------------------------------------------------------------


def test_parked_deferred_with_its_reason_and_no_attempt(queue_dir: Path) -> None:
    add_task(queue_dir, "t", depends_on=["ghost"])

    _select(queue_dir)

    state = _state(queue_dir, "t")
    assert (state.status, state.deferred_reason, state.next_eligible_at) == (
        "deferred",
        GHOST_REASON,
        None,
    )
    assert (state.attempts, state.runs) == (0, [])


def test_warning_once_per_transition(
    queue_dir: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One WARNING and one state write when the task is parked, none on the
    ticks after; un-parked once the task it names exists, and parked with a
    new WARNING when the dependency goes missing again or the missing set
    changes."""
    writes: list[str] = []
    real_write = orchestrator_mod.write_state_atomic

    def counting_write(state: TaskState, path: Path) -> None:
        writes.append(state.task_id)
        real_write(state, path)

    monkeypatch.setattr(orchestrator_mod, "write_state_atomic", counting_write)
    caplog.set_level(logging.INFO, logger=ORCHESTRATOR_LOGGER)
    add_task(queue_dir, "t", depends_on=["ghost"])

    for _ in range(3):
        assert _select(queue_dir)[:2] == ([], Counter({"depends_on_missing": 1}))
    assert _messages(caplog, logging.WARNING) == [_warning("t", GHOST_REASON)]
    assert writes == ["t"]
    caplog.clear()

    # The missing task is created: t is un-parked and waits for it.
    add_task(queue_dir, "ghost")
    assert _select(queue_dir)[:2] == (["ghost"], Counter({"depends_on": 1}))
    state = _state(queue_dir, "t")
    assert (state.status, state.deferred_reason) == ("pending", None)
    assert _messages(caplog, logging.WARNING) == []
    assert _messages(caplog, logging.INFO) == [
        f"task t: un-parked, its hold has cleared: {GHOST_REASON}"
    ]
    caplog.clear()

    # Its task file is removed again: parked again, with a new WARNING.
    task_path_for(queue_dir, "ghost").unlink()
    _select(queue_dir)
    _select(queue_dir)
    assert _messages(caplog, logging.WARNING) == [_warning("t", GHOST_REASON)]
    caplog.clear()

    # A second missing id: a new reason, so one new WARNING naming both.
    add_task(queue_dir, "t", depends_on=["ghost", "phantom"])
    _select(queue_dir)
    _select(queue_dir)
    both = (
        "missing dependency: depends_on names ghost, phantom, but no task in todo/ or "
        "state/ has those ids"
    )
    assert _messages(caplog, logging.WARNING) == [_warning("t", both)]
    assert _state(queue_dir, "t").deferred_reason == both
    assert writes == ["t", "t", "t", "t"]


def test_depends_on_fixed_by_hand_is_eligible_on_the_next_tick(queue_dir: Path) -> None:
    add_task(queue_dir, "t", depends_on=["ghost"])
    _select(queue_dir)

    add_task(queue_dir, "t")

    assert _select(queue_dir)[:2] == (["t"], Counter())
    state = _state(queue_dir, "t")
    assert (state.status, state.deferred_reason) == ("pending", None)


def test_only_its_own_hold_is_cleared(queue_dir: Path) -> None:
    """A deferral someone else wrote stays as it is once the dependency
    exists: an operator's park, without a cooldown."""
    add_task(queue_dir, "upstream")
    add_task(queue_dir, "t", depends_on=["upstream"])
    add_state(queue_dir, "t", "deferred", deferred_reason="parked by hand")

    assert _select(queue_dir)[:2] == (["upstream"], Counter({"depends_on": 1}))
    state = _state(queue_dir, "t")
    assert (state.status, state.deferred_reason) == ("deferred", "parked by hand")


# ---------------------------------------------------------------------------
# Read-only selection
# ---------------------------------------------------------------------------


def test_read_only_selection_writes_and_logs_nothing(
    queue_dir: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same category and missing ids as a tick, but no park, no un-park of
    a hold that has cleared, and no WARNING."""
    add_task(queue_dir, "new-orphan", depends_on=["ghost"])
    add_task(queue_dir, "upstream")
    add_task(queue_dir, "cleared", depends_on=["upstream"])
    add_state(queue_dir, "cleared", "deferred", deferred_reason=GHOST_REASON)
    before = state_path_for(queue_dir, "cleared").read_bytes()
    writes = forbid_selector_writes(monkeypatch)

    with caplog.at_level(logging.DEBUG, logger=ORCHESTRATOR_LOGGER):
        eligible, skips, missing = _select(queue_dir, read_only=True)

    assert writes == []
    assert eligible == ["upstream"]
    assert skips == Counter({"depends_on_missing": 1, "depends_on": 1})
    assert missing == {"new-orphan": ["ghost"]}
    assert _messages(caplog, logging.WARNING) == []
    assert not state_file_path(queue_dir, "new-orphan").exists()
    assert state_path_for(queue_dir, "cleared").read_bytes() == before


def test_preview_selection_reports_the_missing_ids(
    queue_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_task(queue_dir, "t", depends_on=["ghost", "upstream"])
    add_state(queue_dir, "upstream", "pending")
    writes = forbid_selector_writes(monkeypatch)

    preview = orchestrator_mod.preview_selection(
        queue_dir, load_settings(None), snapshot=None, now=NOW
    )

    assert writes == []
    assert preview.skips == Counter({"depends_on_missing": 1})
    assert [t.id for t in preview.held["depends_on_missing"]] == ["t"]
    assert preview.missing_dependencies == {"t": ["ghost"]}
