"""The selector's unmeetable-dependency gate (ADR-0036).

A ``depends_on`` id that names a task that exists can still never complete
while nothing changes: the task's own id, a task in a dependency cycle with
it, or a ``failed_circuit_breaker`` task. The selector holds such a task as
``depends_on_unmeetable``, parks it ``deferred`` with an ``unmeetable
dependency:`` reason and one WARNING per transition, and un-parks it once
the dependency is dropped, the cycle broken or the stopped task reset. A
task that only depends on a cycle member waits under ``depends_on``: the
member is the one held. The cycles are found once per selector pass
(:func:`_dependency_cycles`). A read-only selection reports the same and
writes and logs nothing.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from collections import Counter
from pathlib import Path

import pytest

from claude_task_runner.config.loader import load_defaults, load_settings
from claude_task_runner.queue.schema import Task, TaskState
from claude_task_runner.queue.store import (
    load_state,
    queue_runtime_dir,
    state_file_path,
    state_path_for,
    todo_dir,
)
from claude_task_runner.runner import orchestrator as orchestrator_mod
from claude_task_runner.runner.orchestrator import (
    UnmeetableDependency,
    _dependency_cycles,
    _eligible_candidates,
    _state_task_ids,
)

from ._mixed_queue import add_ring, add_state, add_task, forbid_selector_writes

DEFAULT_LEVELS: dict[str, list[str]] = load_defaults()["effort_levels"]
ORCHESTRATOR_LOGGER = "claude_task_runner.runner.orchestrator"
NOW = dt.datetime(2026, 10, 24, 12, 0, 0, tzinfo=dt.UTC)
FIXES = {
    "self": "drop the task's own id from its depends_on",
    "cycle": "remove one depends_on entry along the cycle",
    "failed_circuit_breaker": (
        "drop the dependency, or set status: pending in the stopped task's state file, "
        ".claude_task_runner/state/<id>.yaml, once the cause of its failures is fixed "
        "(no queue command resets a circuit breaker)"
    ),
}
"""The WARNING's advice per cause, in the order it gives them."""


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    return qd


def _select(
    qd: Path, *, read_only: bool = False
) -> tuple[list[str], Counter[str], dict[str, list[UnmeetableDependency]]]:
    """One selector pass as a supervisor tick makes it."""
    ids = _state_task_ids(qd)
    skips: Counter[str] = Counter()
    unmeetable: dict[str, list[UnmeetableDependency]] = {}
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
        unmeetable_dependencies=unmeetable,
    )
    return sorted(t.id for t in eligible), skips, unmeetable


def _state(qd: Path, task_id: str) -> TaskState:
    return load_state(state_path_for(qd, task_id))


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == ORCHESTRATOR_LOGGER and r.levelno == logging.WARNING
    ]


def _reason(*described: str) -> str:
    return "unmeetable dependency: depends_on names " + "; ".join(described)


def _warning(task_id: str, reason: str, *causes: str) -> str:
    advice = "; or ".join(fix for cause, fix in FIXES.items() if cause in causes)
    return f"parking task {task_id} as deferred: {reason}. To release it, {advice}"


def _task(task_id: str, *depends_on: str) -> Task:
    return Task(id=task_id, title=task_id, prompt="p", depends_on=list(depends_on))


def _members(tasks: list[Task], may_finish: set[str]) -> dict[str, tuple[str, ...]]:
    """:func:`_dependency_cycles` as each task's cycle's sorted members,
    checking that the members of a cycle share one cycle object whose
    membership set holds the same tasks."""
    cycles = _dependency_cycles(tasks, may_finish)
    for cycle in {id(c): c for c in cycles.values()}.values():
        assert cycle.ids == frozenset(cycle.members)
        assert all(cycles[m] is cycle for m in cycle.members)
    return {task_id: cycle.members for task_id, cycle in cycles.items()}


# ---------------------------------------------------------------------------
# _dependency_cycles
# ---------------------------------------------------------------------------


def test_cycles_of_one_two_and_three_tasks() -> None:
    tasks = [
        _task("self", "self"),
        _task("p", "q"),
        _task("q", "p"),
        _task("x", "y"),
        _task("y", "z"),
        _task("z", "x"),
        _task("outside", "x"),
        _task("chain-a", "chain-b"),
        _task("chain-b"),
    ]

    assert _members(tasks, set()) == {
        "self": ("self",),
        "p": ("p", "q"),
        "q": ("p", "q"),
        "x": ("x", "y", "z"),
        "y": ("x", "y", "z"),
        "z": ("x", "y", "z"),
    }


def test_a_cycle_with_a_tail_and_two_loops_sharing_a_task() -> None:
    """Two loops through ``hub`` are one strongly connected component; a
    task that leads into it and one it leads to are outside it."""
    tasks = [
        _task("in", "hub"),
        _task("hub", "a", "b", "out"),
        _task("a", "hub"),
        _task("b", "hub"),
        _task("out"),
    ]

    cycle = ("a", "b", "hub")
    assert _members(tasks, set()) == {"a": cycle, "b": cycle, "hub": cycle}


def test_a_completed_task_breaks_the_cycle() -> None:
    """Depending on a completed task is met, so no path runs through it."""
    tasks = [_task("p", "q"), _task("q", "p"), _task("s", "s")]

    assert _members(tasks, {"q", "s"}) == {}


def test_a_member_a_worker_may_finish_breaks_the_cycle(queue_dir: Path) -> None:
    """A member that is running may still complete, so the other member
    waits for it under depends_on."""
    add_task(queue_dir, "a", depends_on=["b"])
    add_task(queue_dir, "b", depends_on=["a"])
    add_state(queue_dir, "b", "running")

    assert _select(queue_dir) == ([], Counter({"depends_on": 1, "running": 1}), {})
    assert not state_file_path(queue_dir, "a").exists()


def test_dependencies_outside_the_tasks_are_ignored() -> None:
    """Ids with no task YAML are the missing-dependency gate's; they are no
    node of the graph."""
    tasks = [_task("p", "ghost", "q"), _task("q", "p", "ghost")]

    assert _members(tasks, set()) == {"p": ("p", "q"), "q": ("p", "q")}


def test_a_long_chain_and_a_long_ring_stay_iterative_and_linear() -> None:
    """Far deeper than the recursion limit; one pass over 20,000 tasks
    stays well within a tick."""
    n = 10_000
    chain = [_task(f"c{i:05d}", f"c{i + 1:05d}") for i in range(n)]
    ring = [_task(f"r{i:05d}", f"r{(i + 1) % n:05d}") for i in range(n)]

    started = time.perf_counter()
    cycles = _members(chain + ring, set())
    elapsed = time.perf_counter() - started

    ring_ids = tuple(sorted(t.id for t in ring))
    assert cycles == dict.fromkeys(ring_ids, ring_ids)
    assert elapsed < 5.0


@pytest.mark.parametrize(
    ("cycle", "described"),
    [
        (("a", "b"), "b (in a dependency cycle among a, b)"),
        (("a", "b", "c"), "b (in a dependency cycle among a, b, c)"),
        (("a", "b", "c", "d"), "b (in a dependency cycle among a, b, c and 1 more)"),
        (tuple(f"t{i:04d}" for i in range(1000)), None),
    ],
    ids=["two", "three", "four", "a-thousand"],
)
def test_a_cycle_is_described_by_its_first_three_tasks(
    cycle: tuple[str, ...], described: str | None
) -> None:
    if described is None:
        described = "b (in a dependency cycle among t0000, t0001, t0002 and 997 more)"
    assert UnmeetableDependency("b", "cycle", cycle).describe() == described


def test_a_long_rings_reasons_stay_short(queue_dir: Path) -> None:
    """Every task of a 1,000-task ring is held, and each reason names three
    of its tasks and how many more, not all of them."""
    n = 1000
    add_ring(queue_dir, n)

    _eligible, skips, unmeetable = _select(queue_dir, read_only=True)

    assert skips == Counter({"depends_on_unmeetable": n})
    reasons = {
        task_id: orchestrator_mod._unmeetable_dependency_reason(deps)
        for task_id, deps in unmeetable.items()
    }
    assert reasons["r0999"] == (
        "unmeetable dependency: depends_on names r0000 (in a dependency cycle among r0000, "
        "r0001, r0002 and 997 more)"
    )
    assert max(len(reason) for reason in reasons.values()) == len(reasons["r0999"])


# ---------------------------------------------------------------------------
# Which category
# ---------------------------------------------------------------------------


def test_a_task_that_depends_on_itself(queue_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
    add_task(queue_dir, "t", depends_on=["t"])
    reason = _reason("t (the task itself)")

    with caplog.at_level(logging.WARNING, logger=ORCHESTRATOR_LOGGER):
        eligible, skips, unmeetable = _select(queue_dir)

    assert (eligible, skips) == ([], Counter({"depends_on_unmeetable": 1}))
    assert unmeetable == {"t": [UnmeetableDependency("t", "self")]}
    assert _warnings(caplog) == [_warning("t", reason, "self")]
    state = _state(queue_dir, "t")
    assert (state.status, state.deferred_reason, state.next_eligible_at) == (
        "deferred",
        reason,
        None,
    )


def test_a_two_task_cycle_holds_both(queue_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
    add_task(queue_dir, "a", depends_on=["b"])
    add_task(queue_dir, "b", depends_on=["a"])

    with caplog.at_level(logging.WARNING, logger=ORCHESTRATOR_LOGGER):
        eligible, skips, unmeetable = _select(queue_dir)

    assert (eligible, skips) == ([], Counter({"depends_on_unmeetable": 2}))
    assert unmeetable == {
        "a": [UnmeetableDependency("b", "cycle", ("a", "b"))],
        "b": [UnmeetableDependency("a", "cycle", ("a", "b"))],
    }
    assert _warnings(caplog) == [
        _warning("a", _reason("b (in a dependency cycle among a, b)"), "cycle"),
        _warning("b", _reason("a (in a dependency cycle among a, b)"), "cycle"),
    ]


def test_a_three_task_cycle_and_a_task_outside_it(
    queue_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Each member names the member it depends on; the task that depends on
    a member from outside waits under depends_on, with no WARNING, since
    breaking the cycle releases it."""
    add_task(queue_dir, "x", depends_on=["y"])
    add_task(queue_dir, "y", depends_on=["z"])
    add_task(queue_dir, "z", depends_on=["x"])
    add_task(queue_dir, "outside", depends_on=["x"])
    cycle = ("x", "y", "z")

    with caplog.at_level(logging.WARNING, logger=ORCHESTRATOR_LOGGER):
        eligible, skips, unmeetable = _select(queue_dir)

    assert eligible == []
    assert skips == Counter({"depends_on_unmeetable": 3, "depends_on": 1})
    assert unmeetable == {
        "x": [UnmeetableDependency("y", "cycle", cycle)],
        "y": [UnmeetableDependency("z", "cycle", cycle)],
        "z": [UnmeetableDependency("x", "cycle", cycle)],
    }
    assert [w.split(" as deferred")[0] for w in _warnings(caplog)] == [
        "parking task x",
        "parking task y",
        "parking task z",
    ]
    assert not state_file_path(queue_dir, "outside").exists()


def test_a_circuit_broken_dependency(queue_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
    add_task(queue_dir, "upstream")
    add_state(queue_dir, "upstream", "failed_circuit_breaker")
    add_task(queue_dir, "t", depends_on=["upstream"])
    reason = _reason("upstream (failed_circuit_breaker)")

    with caplog.at_level(logging.WARNING, logger=ORCHESTRATOR_LOGGER):
        eligible, skips, unmeetable = _select(queue_dir)

    assert (eligible, skips) == (
        [],
        Counter({"depends_on_unmeetable": 1, "failed_circuit_breaker": 1}),
    )
    assert unmeetable == {"t": [UnmeetableDependency("upstream", "failed_circuit_breaker")]}
    assert _warnings(caplog) == [_warning("t", reason, "failed_circuit_breaker")]


def test_every_unmeetable_dependency_is_named_once_in_depends_on_order(
    queue_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A met and an unfinished dependency beside them are left out; the
    WARNING gives the advice for each cause once, in a fixed order."""
    add_task(queue_dir, "broken")
    add_state(queue_dir, "broken", "failed_circuit_breaker")
    add_task(queue_dir, "done")
    add_state(queue_dir, "done", "completed")
    add_task(queue_dir, "pending-dep")
    add_task(queue_dir, "b", depends_on=["t"])
    add_task(queue_dir, "t", depends_on=["broken", "done", "t", "pending-dep", "b", "broken"])

    with caplog.at_level(logging.WARNING, logger=ORCHESTRATOR_LOGGER):
        _select(queue_dir)

    reason = _reason(
        "broken (failed_circuit_breaker)",
        "t (the task itself)",
        "b (in a dependency cycle among b, t)",
    )
    assert _state(queue_dir, "t").deferred_reason == reason
    assert _warnings(caplog)[-1] == _warning("t", reason, "self", "cycle", "failed_circuit_breaker")


def test_a_missing_dependency_is_reported_first(queue_dir: Path) -> None:
    add_task(queue_dir, "t", depends_on=["t", "ghost"])

    assert _select(queue_dir)[:2] == ([], Counter({"depends_on_missing": 1}))
    assert (_state(queue_dir, "t").deferred_reason or "").startswith("missing dependency: ")


def test_an_unmeetable_dependency_holds_before_an_unfinished_one(queue_dir: Path) -> None:
    add_task(queue_dir, "upstream")
    add_task(queue_dir, "t", depends_on=["upstream", "t"])

    assert _select(queue_dir)[:2] == (
        ["upstream"],
        Counter({"depends_on_unmeetable": 1}),
    )


def test_held_before_the_readiness_gate(queue_dir: Path) -> None:
    add_task(
        queue_dir,
        "t",
        depends_on=["t"],
        requires=[{"kind": "file", "path": "inputs/paper.md"}],
    )

    assert _select(queue_dir)[:2] == ([], Counter({"depends_on_unmeetable": 1}))
    assert _state(queue_dir, "t").deferred_reason == _reason("t (the task itself)")


def test_a_completed_member_ends_the_cycle(queue_dir: Path) -> None:
    """Once one member has completed, the other's dependency is met."""
    add_task(queue_dir, "a", depends_on=["b"])
    add_task(queue_dir, "b", depends_on=["a"])
    add_state(queue_dir, "b", "completed")

    assert _select(queue_dir) == (["a"], Counter({"completed": 1}), {})


# ---------------------------------------------------------------------------
# The WARNING, and un-parking
# ---------------------------------------------------------------------------


def test_warned_once_across_ticks(queue_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
    add_task(queue_dir, "t", depends_on=["t"])

    with caplog.at_level(logging.WARNING, logger=ORCHESTRATOR_LOGGER):
        for _ in range(3):
            _select(queue_dir)

    assert _warnings(caplog) == [_warning("t", _reason("t (the task itself)"), "self")]


def test_dropping_the_self_dependency_releases_the_task(
    queue_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    add_task(queue_dir, "t", depends_on=["t"])
    _select(queue_dir)
    add_task(queue_dir, "t")
    caplog.clear()

    with caplog.at_level(logging.INFO, logger=ORCHESTRATOR_LOGGER):
        assert _select(queue_dir)[:2] == (["t"], Counter())

    state = _state(queue_dir, "t")
    assert (state.status, state.deferred_reason) == ("pending", None)
    assert [r.getMessage() for r in caplog.records if r.name == ORCHESTRATOR_LOGGER] == [
        f"task t: un-parked, its hold has cleared: {_reason('t (the task itself)')}"
    ]


def test_breaking_the_cycle_releases_both(queue_dir: Path) -> None:
    """The member whose depends_on was edited can run; the other now waits
    for it under depends_on, both un-parked."""
    add_task(queue_dir, "a", depends_on=["b"])
    add_task(queue_dir, "b", depends_on=["a"])
    _select(queue_dir)

    add_task(queue_dir, "a")

    assert _select(queue_dir)[:2] == (["a"], Counter({"depends_on": 1}))
    for task_id in ("a", "b"):
        state = _state(queue_dir, task_id)
        assert (state.status, state.deferred_reason) == ("pending", None)


def test_resetting_the_stopped_task_releases_its_dependent(queue_dir: Path) -> None:
    add_task(queue_dir, "upstream")
    add_state(queue_dir, "upstream", "failed_circuit_breaker")
    add_task(queue_dir, "t", depends_on=["upstream"])
    _select(queue_dir)

    add_state(queue_dir, "upstream", "pending")

    assert _select(queue_dir)[:2] == (["upstream"], Counter({"depends_on": 1}))
    state = _state(queue_dir, "t")
    assert (state.status, state.deferred_reason) == ("pending", None)


def test_a_cycle_that_grows_is_warned_again(
    queue_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A new member changes the reason, so the transition is warned."""
    add_task(queue_dir, "a", depends_on=["b"])
    add_task(queue_dir, "b", depends_on=["a"])
    _select(queue_dir)
    add_task(queue_dir, "b", depends_on=["c"])
    add_task(queue_dir, "c", depends_on=["a"])
    caplog.clear()

    with caplog.at_level(logging.WARNING, logger=ORCHESTRATOR_LOGGER):
        _select(queue_dir)
        _select(queue_dir)

    cycle = "in a dependency cycle among a, b, c"
    assert _warnings(caplog) == [
        _warning("a", _reason(f"b ({cycle})"), "cycle"),
        _warning("b", _reason(f"c ({cycle})"), "cycle"),
        _warning("c", _reason(f"a ({cycle})"), "cycle"),
    ]


def test_a_missing_dependency_created_into_a_cycle_changes_the_hold(
    queue_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The missing-dependency hold clears once the task exists, and this
    gate parks the task under its own reason, with its own WARNING."""
    add_task(queue_dir, "a", depends_on=["b"])
    _select(queue_dir)
    assert (_state(queue_dir, "a").deferred_reason or "").startswith("missing dependency: ")
    add_task(queue_dir, "b", depends_on=["a"])
    caplog.clear()

    with caplog.at_level(logging.WARNING, logger=ORCHESTRATOR_LOGGER):
        assert _select(queue_dir)[:2] == ([], Counter({"depends_on_unmeetable": 2}))

    reason = _reason("b (in a dependency cycle among a, b)")
    assert _state(queue_dir, "a").deferred_reason == reason
    assert _warnings(caplog) == [
        _warning("a", reason, "cycle"),
        _warning("b", _reason("a (in a dependency cycle among a, b)"), "cycle"),
    ]


def test_only_its_own_hold_is_cleared(queue_dir: Path) -> None:
    """An operator's park, without a cooldown, is left as it is when no
    dependency is unmeetable."""
    add_task(queue_dir, "a", depends_on=["b"])
    add_state(queue_dir, "a", "deferred", deferred_reason="parked by hand")
    add_task(queue_dir, "b")

    assert _select(queue_dir)[:2] == (["b"], Counter({"depends_on": 1}))
    state = _state(queue_dir, "a")
    assert (state.status, state.deferred_reason) == ("deferred", "parked by hand")


# ---------------------------------------------------------------------------
# Read-only selection, the tick and the preview
# ---------------------------------------------------------------------------


def test_read_only_selection_writes_and_logs_nothing(
    queue_dir: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same categories and dependencies as a tick, no park, no un-park
    of a cleared hold, and no log record at any level. The queue has no
    unparseable file, whose warning a read-only pass would still log."""
    add_task(queue_dir, "a", depends_on=["b"])
    add_task(queue_dir, "b", depends_on=["a"])
    add_task(queue_dir, "cleared")
    add_state(
        queue_dir, "cleared", "deferred", deferred_reason=_reason("cleared (the task itself)")
    )
    before = state_path_for(queue_dir, "cleared").read_bytes()
    writes = forbid_selector_writes(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        eligible, skips, unmeetable = _select(queue_dir, read_only=True)

    assert writes == []
    assert caplog.records == []
    assert (eligible, skips) == (["cleared"], Counter({"depends_on_unmeetable": 2}))
    assert unmeetable == {
        "a": [UnmeetableDependency("b", "cycle", ("a", "b"))],
        "b": [UnmeetableDependency("a", "cycle", ("a", "b"))],
    }
    assert not state_file_path(queue_dir, "a").exists()
    assert state_path_for(queue_dir, "cleared").read_bytes() == before


def test_preview_selection_reports_the_unmeetable_dependencies(
    queue_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_task(queue_dir, "t", depends_on=["t"])
    writes = forbid_selector_writes(monkeypatch)

    preview = orchestrator_mod.preview_selection(
        queue_dir, load_settings(None), snapshot=None, now=NOW
    )

    assert writes == []
    assert preview.skips == Counter({"depends_on_unmeetable": 1})
    assert preview.unmeetable_dependencies == {"t": [UnmeetableDependency("t", "self")]}


def test_given_the_state_ids_the_selector_reads_no_state_directory(
    queue_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tick passes what its one pass over the state files found."""
    add_task(queue_dir, "upstream")
    add_task(queue_dir, "t", depends_on=["upstream"])

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
        may_complete_ids=set(),
        circuit_broken_ids={"upstream"},
    )

    assert skips == Counter({"depends_on_unmeetable": 1})
