"""The selector's missing-dependency gate (ADR-0036).

A ``depends_on`` id names a task that exists when it has a ``todo/`` YAML,
a completed state, or a state a worker may still complete (``running``,
``possibly_hung``, or a state file that does not parse). The task then
waits for it: ``depends_on``. Any other id can never complete: there is no
task by that id, or only a state left behind by a task whose YAML was
renamed or deleted. The selector holds such a task as
``depends_on_missing``, parks it ``deferred`` with a ``missing
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
from claude_task_runner.queue.schema import RunRecord, TaskState
from claude_task_runner.queue.sidecar import write_response
from claude_task_runner.queue.store import (
    load_state,
    queue_runtime_dir,
    state_file_path,
    state_path_for,
    task_path_for,
    todo_dir,
    write_state_atomic,
)
from claude_task_runner.runner import orchestrator as orchestrator_mod
from claude_task_runner.runner.orchestrator import _eligible_candidates, _state_task_ids

from ._mixed_queue import add_state, add_task, forbid_selector_writes
from ._sidecar_files import make_request, make_response, write_request

DEFAULT_LEVELS: dict[str, list[str]] = load_defaults()["effort_levels"]
ORCHESTRATOR_LOGGER = "claude_task_runner.runner.orchestrator"
NOW = dt.datetime(2026, 10, 23, 12, 0, 0, tzinfo=dt.UTC)

GHOST_REASON = (
    "missing dependency: depends_on names ghost, which has no task file in todo/ and is not "
    "completed or running"
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
    """One selector pass as a supervisor tick makes it: the completed ids
    and those that may still complete, from one pass over the state files."""
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
        may_complete_ids=ids.may_complete,
        circuit_broken_ids=ids.circuit_broken,
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


def _run(attempt: int) -> RunRecord:
    return RunRecord(
        attempt=attempt,
        started_at=NOW - dt.timedelta(hours=2),
        finished_at=NOW - dt.timedelta(hours=1),
        stop_reason="error",
        duration_s=3600.0,
    )


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


def _unparseable_todo_yaml(qd: Path, task_id: str) -> None:
    (todo_dir(qd) / f"{task_id}.yaml").write_text("not: valid: yaml: [\n", encoding="utf-8")


def _unparseable_state(qd: Path, task_id: str) -> None:
    state_path_for(qd, task_id).write_text("not yaml: ][[", encoding="utf-8")


def _state_only(status: str) -> Callable[[Path, str], None]:
    def make(qd: Path, task_id: str) -> None:
        add_state(qd, task_id, status)

    return make


@pytest.mark.parametrize(
    ("make_dependency", "expected_eligible", "expected_skips"),
    [
        (_todo_yaml, ["upstream"], Counter({"depends_on": 1})),
        (_unparseable_todo_yaml, [], Counter({"depends_on": 1, "unparseable_task": 1})),
        (_state_only("completed"), ["waits"], Counter()),
        (_state_only("running"), [], Counter({"depends_on": 1})),
        (_state_only("possibly_hung"), [], Counter({"depends_on": 1})),
        (_unparseable_state, [], Counter({"depends_on": 1})),
    ],
    ids=[
        "todo-yaml",
        "unparseable-todo-yaml",
        "state-only-completed",
        "state-only-running",
        "state-only-possibly-hung",
        "state-only-unparseable",
    ],
)
def test_a_dependency_that_exists(
    queue_dir: Path,
    caplog: pytest.LogCaptureFixture,
    make_dependency: Callable[[Path, str], None],
    expected_eligible: list[str],
    expected_skips: Counter[str],
) -> None:
    """A todo/ YAML, readable or not, a completed state, a state a worker
    may still complete, or a state that cannot be read: the waiting task
    waits for it (or runs, once it completed), unparked and without a
    WARNING. A circuit-broken task with a todo/ YAML exists too, but cannot
    complete: tests/unit/test_orchestrator_unmeetable_dependency.py."""
    make_dependency(queue_dir, "upstream")
    add_task(queue_dir, "waits", depends_on=["upstream"])

    with caplog.at_level(logging.WARNING, logger=ORCHESTRATOR_LOGGER):
        eligible, skips, missing = _select(queue_dir)

    assert (eligible, skips, missing) == (expected_eligible, expected_skips, {})
    assert not state_file_path(queue_dir, "waits").exists()
    # An unreadable file is warned about as such; the gate warns of nothing.
    assert [m for m in _messages(caplog, logging.WARNING) if m.startswith("parking")] == []


@pytest.mark.parametrize(
    "status",
    [
        "pending",
        "failed",
        "deferred",
        "awaiting_sidecar",
        "failed_circuit_breaker",
        "weekly_paused",
    ],
)
def test_a_state_left_without_its_task_file_is_missing(
    queue_dir: Path, caplog: pytest.LogCaptureFixture, status: str
) -> None:
    """A state with no todo/ YAML, such as one left behind when the task's
    YAML was renamed or deleted, is never dispatched again unless a worker
    may still be running it, so a task waiting for it is held as missing."""
    add_state(queue_dir, "upstream", status)
    add_task(queue_dir, "waits", depends_on=["upstream"])
    reason = (
        "missing dependency: depends_on names upstream, which has no task file in todo/ and is "
        "not completed or running"
    )

    with caplog.at_level(logging.WARNING, logger=ORCHESTRATOR_LOGGER):
        eligible, skips, missing = _select(queue_dir)

    assert (eligible, skips, missing) == (
        [],
        Counter({"depends_on_missing": 1}),
        {"waits": ["upstream"]},
    )
    assert _messages(caplog, logging.WARNING) == [_warning("waits", reason)]


def test_a_completed_task_in_done_still_counts(queue_dir: Path) -> None:
    """Some queues move a finished task's YAML to done/; its completed state
    still satisfies the dependency. done/ is not read: a task there whose
    state is not completed is missing."""
    done = queue_dir / "done"
    done.mkdir()
    for task_id, status in (("finished", "completed"), ("abandoned", "failed")):
        add_task(queue_dir, task_id)
        task_path_for(queue_dir, task_id).rename(done / f"{task_id}.yaml")
        add_state(queue_dir, task_id, status)
    add_task(queue_dir, "after-finished", depends_on=["finished"])
    add_task(queue_dir, "after-abandoned", depends_on=["abandoned"])

    eligible, skips, missing = _select(queue_dir)

    assert eligible == ["after-finished"]
    assert skips == Counter({"depends_on_missing": 1})
    assert missing == {"after-abandoned": ["abandoned"]}


def test_a_missing_dependency_wins_over_an_unfinished_one(
    queue_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Waiting cannot clear a missing dependency, so it is the hold
    reported. The missing ids are listed once each, in depends_on order."""
    add_task(queue_dir, "upstream")
    add_task(queue_dir, "t", depends_on=["upstream", "ghost-b", "ghost-a", "ghost-b"])
    reason = (
        "missing dependency: depends_on names ghost-b, ghost-a, which have no task file in todo/ "
        "and are not completed or running"
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


def test_given_the_ids_that_may_complete_the_selector_reads_no_state_directory(
    queue_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tick passes the ids its one pass over the state files found; the
    selector takes those, without a pass of its own."""
    add_task(queue_dir, "t", depends_on=["running-elsewhere"])

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
        may_complete_ids={"running-elsewhere"},
        circuit_broken_ids=set(),
    )

    assert skips == Counter({"depends_on": 1})


# ---------------------------------------------------------------------------
# Order among the gates
# ---------------------------------------------------------------------------


def test_held_as_missing_before_the_readiness_gate(
    queue_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A task with a missing dependency and an unmet requirement is held,
    and parked, for the dependency; the readiness gate is not reached."""
    add_task(
        queue_dir,
        "t",
        depends_on=["ghost"],
        requires=[{"kind": "file", "path": "inputs/paper.md"}],
    )

    with caplog.at_level(logging.WARNING, logger=ORCHESTRATOR_LOGGER):
        assert _select(queue_dir)[:2] == ([], Counter({"depends_on_missing": 1}))

    assert _state(queue_dir, "t").deferred_reason == GHOST_REASON
    assert _messages(caplog, logging.WARNING) == [_warning("t", GHOST_REASON)]


def test_an_unfinished_dependency_holds_before_the_readiness_gate(queue_dir: Path) -> None:
    """With the dependency present but unfinished, the task waits for it;
    the readiness gate is not reached, so it does not park the task."""
    add_task(queue_dir, "upstream")
    add_task(
        queue_dir,
        "t",
        depends_on=["upstream"],
        requires=[{"kind": "file", "path": "inputs/paper.md"}],
    )

    assert _select(queue_dir)[:2] == (["upstream"], Counter({"depends_on": 1}))
    assert not state_file_path(queue_dir, "t").exists()


def test_a_hook_cooldown_holds_first_and_is_left_alone(
    queue_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Inside a pre-dispatch hook's cooldown the task is held for the
    cooldown; the missing dependency is not looked at until it passes."""
    add_task(queue_dir, "t", depends_on=["ghost"])
    add_state(
        queue_dir,
        "t",
        "deferred",
        deferred_reason="pre-dispatch hook deferred (exit 1): waiting",
        next_eligible_at=NOW + dt.timedelta(hours=1),
    )
    before = state_path_for(queue_dir, "t").read_bytes()

    with caplog.at_level(logging.WARNING, logger=ORCHESTRATOR_LOGGER):
        assert _select(queue_dir) == ([], Counter({"deferred_cooldown": 1}), {})

    assert state_path_for(queue_dir, "t").read_bytes() == before
    assert _messages(caplog, logging.WARNING) == []


def test_a_task_parked_by_the_readiness_gate_is_reparked_for_the_dependency(
    queue_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A readiness hold has no cooldown, so the task reaches this gate: the
    missing dependency replaces the readiness reason, with a WARNING. Once
    the dependency is fixed the readiness gate parks it under its own
    reason again."""
    requires = [{"kind": "file", "path": "inputs/paper.md"}]
    readiness_reason = f"readiness hold: missing file: {queue_dir / 'inputs' / 'paper.md'}"
    add_task(queue_dir, "t", requires=requires)
    add_state(queue_dir, "t", "deferred", deferred_reason=readiness_reason)
    add_task(queue_dir, "t", depends_on=["ghost"], requires=requires)

    with caplog.at_level(logging.WARNING, logger=ORCHESTRATOR_LOGGER):
        assert _select(queue_dir)[:2] == ([], Counter({"depends_on_missing": 1}))
    assert _state(queue_dir, "t").deferred_reason == GHOST_REASON
    assert _messages(caplog, logging.WARNING) == [_warning("t", GHOST_REASON)]

    add_task(queue_dir, "t", requires=requires)
    assert _select(queue_dir)[:2] == ([], Counter({"readiness_missing_file": 1}))
    state = _state(queue_dir, "t")
    assert (state.status, state.deferred_reason) == ("deferred", readiness_reason)


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
        "missing dependency: depends_on names ghost, phantom, which have no task file in todo/ "
        "and are not completed or running"
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


def test_park_and_unpark_keep_the_session_attempts_and_runs(queue_dir: Path) -> None:
    """A park and an un-park change the status and the reason only: the
    session to resume, the attempt count and the run history stay."""
    add_task(queue_dir, "t", depends_on=["ghost"])
    write_state_atomic(
        TaskState(
            task_id="t",
            status="pending",
            attempts=2,
            runs=[_run(1), _run(2)],
            session_id="session-t",
            session_account="work",
        ),
        state_path_for(queue_dir, "t"),
    )
    before = _state(queue_dir, "t")

    _select(queue_dir)
    parked = _state(queue_dir, "t")
    add_task(queue_dir, "t")
    _select(queue_dir)
    unparked = _state(queue_dir, "t")

    assert (parked.status, parked.deferred_reason) == ("deferred", GHOST_REASON)
    assert unparked == before


@pytest.mark.parametrize("status", ["failed", "awaiting_sidecar"])
def test_a_resumable_task_gaining_a_missing_dependency(
    queue_dir: Path, caplog: pytest.LogCaptureFixture, status: str
) -> None:
    """A failed task, or one whose questions are all answered, would be
    dispatched again; with a missing dependency it is parked instead, and
    un-parked as ``pending`` (not as the status it had), its session kept,
    as the effort and readiness gates do."""
    add_task(queue_dir, "t", depends_on=["ghost"])
    write_state_atomic(
        TaskState(task_id="t", status=status, attempts=1, session_id="session-t"),
        state_path_for(queue_dir, "t"),
    )
    if status == "awaiting_sidecar":
        write_request(queue_dir, make_request("t"))
        write_response(queue_dir, make_response("t"))

    with caplog.at_level(logging.WARNING, logger=ORCHESTRATOR_LOGGER):
        assert _select(queue_dir)[:2] == ([], Counter({"depends_on_missing": 1}))
    parked = _state(queue_dir, "t")
    assert (parked.status, parked.deferred_reason) == ("deferred", GHOST_REASON)
    assert _messages(caplog, logging.WARNING) == [_warning("t", GHOST_REASON)]

    add_task(queue_dir, "t")
    assert _select(queue_dir)[:2] == (["t"], Counter())
    state = _state(queue_dir, "t")
    assert (state.status, state.deferred_reason, state.attempts, state.session_id) == (
        "pending",
        None,
        1,
        "session-t",
    )


def test_an_unanswered_sidecar_holds_first(queue_dir: Path) -> None:
    """A task still waiting for an answer is held for it, untouched."""
    add_task(queue_dir, "t", depends_on=["ghost"])
    add_state(queue_dir, "t", "awaiting_sidecar")
    write_request(queue_dir, make_request("t"))
    before = state_path_for(queue_dir, "t").read_bytes()

    assert _select(queue_dir) == ([], Counter({"unanswered_sidecar": 1}), {})
    assert state_path_for(queue_dir, "t").read_bytes() == before


# ---------------------------------------------------------------------------
# Read-only selection
# ---------------------------------------------------------------------------


def test_read_only_selection_writes_and_logs_nothing(
    queue_dir: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same category and missing ids as a tick, but no park, no un-park of
    a hold that has cleared, and no log record at any level. The queue has
    no unparseable file, whose warning a read-only pass would still log."""
    add_task(queue_dir, "new-orphan", depends_on=["ghost"])
    add_task(queue_dir, "upstream")
    add_task(queue_dir, "cleared", depends_on=["upstream"])
    add_state(queue_dir, "cleared", "deferred", deferred_reason=GHOST_REASON)
    before = state_path_for(queue_dir, "cleared").read_bytes()
    writes = forbid_selector_writes(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        eligible, skips, missing = _select(queue_dir, read_only=True)

    assert writes == []
    assert eligible == ["upstream"]
    assert skips == Counter({"depends_on_missing": 1, "depends_on": 1})
    assert missing == {"new-orphan": ["ghost"]}
    assert caplog.records == []
    assert not state_file_path(queue_dir, "new-orphan").exists()
    assert state_path_for(queue_dir, "cleared").read_bytes() == before


def test_preview_selection_reports_the_missing_ids(
    queue_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_task(queue_dir, "t", depends_on=["ghost", "upstream"])
    add_state(queue_dir, "upstream", "running")
    writes = forbid_selector_writes(monkeypatch)

    preview = orchestrator_mod.preview_selection(
        queue_dir, load_settings(None), snapshot=None, now=NOW
    )

    assert writes == []
    assert preview.skips == Counter({"depends_on_missing": 1})
    assert [t.id for t in preview.held["depends_on_missing"]] == ["t"]
    assert preview.missing_dependencies == {"t": ["ghost"]}
