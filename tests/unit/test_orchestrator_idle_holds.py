"""Why the selector admitted nothing: the skip breakdown and its log line.

``_eligible_candidates`` counts every task YAML it does not return under the
category that held it, from the same per-task decision that refuses it
(``_skip_category``). ``tick_dispatch`` hands those counts to the supervisor's
``IdleDispatchLog``, which logs one INFO line when a tick that reached
candidate selection found nothing to dispatch, stays quiet while the
breakdown is unchanged, and logs again when it changes or when tasks become
eligible. Without the line, a drained queue and a stalled supervisor leave
the same silent log.
"""

from __future__ import annotations

import functools
import logging
import threading
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from claude_task_runner.clock import FakeClock
from claude_task_runner.config.loader import load_defaults, load_settings
from claude_task_runner.config.schema import (
    AccountConcurrencyPolicy,
    AccountPolicy,
    ResolvedAccount,
)
from claude_task_runner.queue.schema import (
    CURRENT_SCHEMA_VERSION,
    SidecarAnswer,
    SidecarOption,
    SidecarQuestion,
    SidecarRequest,
    SidecarResponse,
    Task,
    TaskState,
)
from claude_task_runner.queue.sidecar import write_response
from claude_task_runner.queue.store import (
    queue_runtime_dir,
    state_path_for,
    task_path_for,
    todo_dir,
    write_state_atomic,
    write_task_atomic,
)
from claude_task_runner.runner import orchestrator as orchestrator_mod
from claude_task_runner.runner.in_flight import DispatchSlot
from claude_task_runner.runner.orchestrator import IdleDispatchLog, tick_dispatch
from claude_task_runner.supervisor import daemon as daemon_mod
from claude_task_runner.supervisor.daemon import start_daemon
from claude_task_runner.supervisor.states import (
    AccountState,
    SupervisorSnapshot,
    SupervisorState,
)
from claude_task_runner.usage.models import UsageReading, WindowReading
from claude_task_runner.usage.source import FakeUsageSource

from ._sidecar_files import write_request

ORCHESTRATOR_LOGGER = "claude_task_runner.runner.orchestrator"
NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
BLOCK_FILE = "needs_acquisition.jsonl"

_eligible_candidates = functools.partial(
    orchestrator_mod._eligible_candidates, effort_levels=load_defaults()["effort_levels"]
)


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    return qd


# --- queue builders -----------------------------------------------------


def _task(qd: Path, task_id: str, **fields: Any) -> Task:
    task = Task.model_validate(
        {"id": task_id, "title": f"Task {task_id}", "prompt": "do the thing", **fields}
    )
    write_task_atomic(task, task_path_for(qd, task_id))
    return task


def _state(qd: Path, task_id: str, status: str, **fields: Any) -> None:
    state = TaskState.model_validate({"task_id": task_id, "status": status, **fields})
    write_state_atomic(state, state_path_for(qd, task_id))


def _request(task_id: str, seq: int = 1) -> SidecarRequest:
    return SidecarRequest(
        schema_version=CURRENT_SCHEMA_VERSION,
        task_id=task_id,
        sequence=seq,
        created_at=datetime(2026, 10, 1, tzinfo=UTC),
        summary="placeholder",
        context="placeholder",
        questions=[
            SidecarQuestion(
                id="q1",
                prompt="proceed?",
                options=[
                    SidecarOption(value="A", label="yes", description=""),
                    SidecarOption(value="B", label="no", description=""),
                ],
                recommended="A",
                multi_select=False,
            )
        ],
    )


def _response(task_id: str, seq: int = 1) -> SidecarResponse:
    return SidecarResponse(
        schema_version=CURRENT_SCHEMA_VERSION,
        task_id=task_id,
        sequence=seq,
        responded_at=datetime(2026, 10, 2, tzinfo=UTC),
        answers=[SidecarAnswer(id="q1", value="A")],
        notes="",
    )


def _build_mixed_queue(qd: Path, missing_file: Path) -> None:
    """One task per way the selector can hold a task, plus completed and
    eligible ones. ``missing_file`` is an absolute path that does not exist,
    so two copies of this queue write identical readiness reasons."""
    (todo_dir(qd) / "broken.yaml").write_text("not: valid: yaml: [\n", encoding="utf-8")

    # block_dispatch row; the second id's task is completed, so it counts
    # as completed rather than held.
    _task(qd, "blocked")
    _task(qd, "blocked-done")
    _state(qd, "blocked-done", "completed")
    (qd / BLOCK_FILE).write_text(
        '{"task": "blocked", "block_dispatch": true}\n'
        '{"task": "blocked-done", "block_dispatch": true}\n',
        encoding="utf-8",
    )

    _task(qd, "inflight")

    _task(qd, "corrupt-state")
    state_path_for(qd, "corrupt-state").write_text("not yaml: ][[", encoding="utf-8")

    _task(qd, "sidecar-open")
    _state(qd, "sidecar-open", "awaiting_sidecar")
    write_request(qd, _request("sidecar-open"))

    _task(qd, "cooldown")
    _state(
        qd,
        "cooldown",
        "deferred",
        deferred_reason="pre-dispatch hook deferred (exit 1): waiting",
        next_eligible_at=NOW + timedelta(days=365),
    )

    for status in ("running", "possibly_hung", "failed_circuit_breaker", "weekly_paused"):
        _task(qd, f"status-{status}")
        _state(qd, f"status-{status}", status)

    _task(qd, "done")
    _state(qd, "done", "completed")

    _task(qd, "bad-effort", model="claude-sonnet-4-6", effort="max")

    _task(qd, "waits-on-upstream", depends_on=["never-finishes"])

    _task(qd, "needs-file", requires=[{"kind": "file", "path": str(missing_file)}])

    _task(qd, "needs-answer", requires=[{"kind": "sidecar_response"}])
    write_request(qd, _request("needs-answer"))

    _task(
        qd,
        "needs-both",
        requires=[{"kind": "sidecar_response"}, {"kind": "file", "path": str(missing_file)}],
    )
    write_request(qd, _request("needs-both"))

    # Eligible: no state, a deferral past its cooldown, an answered sidecar.
    _task(qd, "ready")
    _task(qd, "cooldown-over")
    _state(
        qd,
        "cooldown-over",
        "deferred",
        deferred_reason="pre-dispatch hook deferred (exit 1): waiting",
        next_eligible_at=NOW - timedelta(minutes=1),
    )
    _task(qd, "sidecar-answered")
    _state(qd, "sidecar-answered", "awaiting_sidecar")
    write_request(qd, _request("sidecar-answered"))
    write_response(qd, _response("sidecar-answered"))


MIXED_EXPECTED_SKIPS = Counter(
    {
        "unparseable_task": 1,
        "block_dispatch": 1,
        "in_flight": 1,
        "unparseable_state": 1,
        "unanswered_sidecar": 1,
        "deferred_cooldown": 1,
        "running": 1,
        "possibly_hung": 1,
        "failed_circuit_breaker": 1,
        "weekly_paused": 1,
        "invalid_effort": 1,
        "depends_on": 1,
        "readiness_missing_file": 1,
        "readiness_awaiting_sidecar_response": 1,
        "readiness_awaiting_sidecar_response+missing_file": 1,
        "completed": 2,
    }
)
MIXED_EXPECTED_ELIGIBLE = ["cooldown-over", "ready", "sidecar-answered"]


def _in_flight(task_id: str) -> dict[str, DispatchSlot]:
    return {
        task_id: DispatchSlot(
            task_id=task_id,
            account="default",
            started_at=NOW,
            thread=threading.Thread(target=lambda: None),
        )
    }


def _select(qd: Path, skips: Counter[str] | None) -> list[str]:
    eligible = _eligible_candidates(
        qd,
        _in_flight("inflight"),
        {"blocked-done", "done"},
        now=NOW,
        block_file=BLOCK_FILE,
        skips=skips,
    )
    return sorted(t.id for t in eligible)


# --- the breakdown --------------------------------------------------------


def test_every_hold_category_is_counted_exactly(queue_dir: Path, tmp_path: Path) -> None:
    _build_mixed_queue(queue_dir, tmp_path / "absent" / "paper.md")
    skips: Counter[str] = Counter()

    assert _select(queue_dir, skips) == MIXED_EXPECTED_ELIGIBLE
    assert skips == MIXED_EXPECTED_SKIPS


def test_every_task_yaml_is_eligible_or_counted(queue_dir: Path, tmp_path: Path) -> None:
    """Returned tasks plus counted skips account for every YAML in todo/.

    A skip that records no category makes the sum fall short."""
    _build_mixed_queue(queue_dir, tmp_path / "absent" / "paper.md")
    skips: Counter[str] = Counter()

    eligible = _select(queue_dir, skips)

    n_yamls = len(list(todo_dir(queue_dir).glob("*.yaml")))
    assert n_yamls == 20
    assert len(eligible) + sum(skips.values()) == n_yamls


def test_breakdown_is_stable_across_ticks(queue_dir: Path, tmp_path: Path) -> None:
    """The first pass parks held tasks as deferred; the second pass must
    still put each under the gate that holds it, not under the park."""
    _build_mixed_queue(queue_dir, tmp_path / "absent" / "paper.md")
    _select(queue_dir, Counter())
    skips: Counter[str] = Counter()

    assert _select(queue_dir, skips) == MIXED_EXPECTED_ELIGIBLE
    assert skips == MIXED_EXPECTED_SKIPS


def test_counting_skips_has_no_side_effects(tmp_path: Path) -> None:
    """Counting is side-effect free: the same queue selected once without
    ``skips`` and once with returns the same tasks, and the gates write
    byte-identical state files."""
    missing = tmp_path / "absent" / "paper.md"
    without, with_skips = tmp_path / "without", tmp_path / "with"
    for qd in (without, with_skips):
        qd.mkdir()
        queue_runtime_dir(qd)
        todo_dir(qd)
        _build_mixed_queue(qd, missing)

    assert _select(without, None) == _select(with_skips, Counter())

    def states(qd: Path) -> dict[str, bytes]:
        return {p.name: p.read_bytes() for p in state_path_for(qd, "x").parent.glob("*.yaml")}

    assert states(without) == states(with_skips)
    assert set(states(without)) >= {"needs-file.yaml", "bad-effort.yaml"}


def test_completed_whichever_check_skips_it(queue_dir: Path) -> None:
    """A completed task the block-list or the in-flight set skips first is
    still reported as completed, never as held."""
    _task(queue_dir, "blocked-done")
    _task(queue_dir, "inflight-done")
    for tid in ("blocked-done", "inflight-done"):
        _state(queue_dir, tid, "completed")
    (queue_dir / BLOCK_FILE).write_text(
        '{"task": "blocked-done", "block_dispatch": true}\n', encoding="utf-8"
    )
    skips: Counter[str] = Counter()

    eligible = _eligible_candidates(
        queue_dir,
        _in_flight("inflight-done"),
        {"blocked-done", "inflight-done"},
        now=NOW,
        block_file=BLOCK_FILE,
        skips=skips,
    )

    assert eligible == []
    assert skips == Counter({"completed": 2})


def test_readiness_category_ignores_requires_order(queue_dir: Path, tmp_path: Path) -> None:
    missing = str(tmp_path / "absent.md")
    _task(
        queue_dir, "a", requires=[{"kind": "file", "path": missing}, {"kind": "sidecar_response"}]
    )
    _task(
        queue_dir, "b", requires=[{"kind": "sidecar_response"}, {"kind": "file", "path": missing}]
    )
    write_request(queue_dir, _request("a"))
    write_request(queue_dir, _request("b"))
    skips: Counter[str] = Counter()

    assert _eligible_candidates(queue_dir, {}, set(), now=NOW, skips=skips) == []
    assert skips == Counter({"readiness_awaiting_sidecar_response+missing_file": 2})


# --- the summary line ---------------------------------------------------


def test_summary_lists_largest_first_then_by_name() -> None:
    line = orchestrator_mod._idle_summary(
        orchestrator_mod._unfinished_skips(
            Counter(
                {
                    "block_dispatch": 178,
                    "readiness_missing_file": 128,
                    "unanswered_sidecar": 18,
                    "depends_on": 3,
                    "possibly_hung": 1,
                    "deferred_cooldown": 1,
                    "completed": 5071,
                }
            )
        )
    )
    assert line == (
        "no eligible tasks: 329 unfinished held (block_dispatch 178, "
        "readiness_missing_file 128, unanswered_sidecar 18, depends_on 3, "
        "deferred_cooldown 1, possibly_hung 1)"
    )


def test_summary_when_only_completed_tasks_remain() -> None:
    breakdown = orchestrator_mod._unfinished_skips(Counter({"completed": 12}))
    assert breakdown == {}
    assert (
        orchestrator_mod._idle_summary(breakdown)
        == "no eligible tasks: no unfinished tasks in todo/"
    )


# --- IdleDispatchLog through tick_dispatch --------------------------------


def _settings(max_c: int = 5) -> Any:
    return SimpleNamespace(
        concurrency=SimpleNamespace(initial_concurrency=max_c, max_concurrency=max_c),
        task_caps=SimpleNamespace(),
        session=SimpleNamespace(),
        effort_levels=load_defaults()["effort_levels"],
        hooks=SimpleNamespace(),
        failure_classifier=None,
        dispatch=SimpleNamespace(auto_detect_paths_in_prompt=False),
        claude=SimpleNamespace(config_dir=""),
    )


def _snapshot(state: SupervisorState = SupervisorState.DISPATCHING) -> SupervisorSnapshot:
    return SupervisorSnapshot(
        state=state,
        since=NOW,
        accounts={"default": AccountState(state=state, since=NOW)},
    )


_ACCOUNTS = [
    ResolvedAccount(
        name="default",
        config_dir="",
        policy=AccountPolicy(concurrency=AccountConcurrencyPolicy(max_concurrency=5)),
    )
]


@pytest.fixture
def no_spawn(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Dispatch threads run a no-op instead of spawning a worker."""
    monkeypatch.setattr(orchestrator_mod, "_dispatch_one_safely", lambda *a, **k: None)
    yield


def _tick(
    qd: Path,
    idle_log: IdleDispatchLog,
    slots: dict[str, DispatchSlot] | None = None,
    *,
    snapshot: SupervisorSnapshot | None = None,
    max_c: int = 5,
) -> dict[str, DispatchSlot]:
    in_flight = {} if slots is None else slots
    tick_dispatch(
        queue_dir=qd,
        settings=_settings(max_c),
        clock=FakeClock(NOW),
        snapshot=snapshot or _snapshot(),
        in_flight_slots=in_flight,
        accounts=_ACCOUNTS,
        idle_log=idle_log,
    )
    for slot in in_flight.values():
        slot.thread.join(timeout=2)
    return in_flight


def _idle_lines(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    lines = [
        (r.levelno, r.getMessage())
        for r in caplog.records
        if r.name == ORCHESTRATOR_LOGGER
        and r.getMessage().startswith(("no eligible tasks", "eligible tasks available again"))
    ]
    caplog.clear()
    return lines


def test_logs_on_entry_change_and_recovery_only(
    queue_dir: Path, caplog: pytest.LogCaptureFixture, no_spawn: None
) -> None:
    caplog.set_level(logging.INFO, logger=ORCHESTRATOR_LOGGER)
    idle_log = IdleDispatchLog()
    _task(queue_dir, "waits", depends_on=["never-finishes"])

    _tick(queue_dir, idle_log)
    assert _idle_lines(caplog) == [
        (logging.INFO, "no eligible tasks: 1 unfinished held (depends_on 1)")
    ]

    # Same breakdown: silent.
    _tick(queue_dir, idle_log)
    assert _idle_lines(caplog) == []

    # Breakdown changes: logged again.
    _task(queue_dir, "later")
    _state(queue_dir, "later", "deferred", next_eligible_at=NOW + timedelta(hours=1))
    _tick(queue_dir, idle_log)
    assert _idle_lines(caplog) == [
        (
            logging.INFO,
            "no eligible tasks: 2 unfinished held (deferred_cooldown 1, depends_on 1)",
        )
    ]
    _tick(queue_dir, idle_log)
    assert _idle_lines(caplog) == []

    # A candidate appears: one recovery line, and it is dispatched.
    _task(queue_dir, "ready")
    slots = _tick(queue_dir, idle_log)
    assert _idle_lines(caplog) == [(logging.INFO, "eligible tasks available again: 1")]
    assert list(slots) == ["ready"]

    # Still eligible next tick (the no-op dispatch left it pending): no line.
    _tick(queue_dir, idle_log, slots)
    assert _idle_lines(caplog) == []

    # Empty again with the breakdown first logged: a new spell logs it anew.
    task_path_for(queue_dir, "ready").unlink()
    _tick(queue_dir, idle_log)
    assert _idle_lines(caplog) == [
        (
            logging.INFO,
            "no eligible tasks: 2 unfinished held (deferred_cooldown 1, depends_on 1)",
        )
    ]


def test_completed_tasks_change_nothing_in_the_line(
    queue_dir: Path, caplog: pytest.LogCaptureFixture, no_spawn: None
) -> None:
    caplog.set_level(logging.INFO, logger=ORCHESTRATOR_LOGGER)
    idle_log = IdleDispatchLog()
    _task(queue_dir, "done-1")
    _state(queue_dir, "done-1", "completed")

    _tick(queue_dir, idle_log)
    assert _idle_lines(caplog) == [
        (logging.INFO, "no eligible tasks: no unfinished tasks in todo/")
    ]

    # Another task completes: the unfinished breakdown is unchanged.
    _task(queue_dir, "done-2")
    _state(queue_dir, "done-2", "completed")
    _tick(queue_dir, idle_log)
    assert _idle_lines(caplog) == []


def test_in_flight_alone_is_not_a_change(caplog: pytest.LogCaptureFixture) -> None:
    """``in_flight`` moves whenever a running task starts or finishes; it is
    printed, but only a change in another category logs the line again."""
    caplog.set_level(logging.INFO, logger=ORCHESTRATOR_LOGGER)
    idle_log = IdleDispatchLog()

    idle_log.observe(0, Counter({"depends_on": 1}))
    idle_log.observe(0, Counter({"depends_on": 1, "in_flight": 1}))
    idle_log.observe(0, Counter({"depends_on": 1, "in_flight": 2}))
    idle_log.observe(0, Counter({"depends_on": 1}))
    assert _idle_lines(caplog) == [
        (logging.INFO, "no eligible tasks: 1 unfinished held (depends_on 1)")
    ]

    idle_log.observe(0, Counter({"depends_on": 1, "in_flight": 1, "deferred_cooldown": 1}))
    assert _idle_lines(caplog) == [
        (
            logging.INFO,
            "no eligible tasks: 3 unfinished held (deferred_cooldown 1, depends_on 1, in_flight 1)",
        )
    ]


def test_silent_when_no_account_can_dispatch(
    queue_dir: Path, caplog: pytest.LogCaptureFixture, no_spawn: None
) -> None:
    caplog.set_level(logging.INFO, logger=ORCHESTRATOR_LOGGER)
    idle_log = IdleDispatchLog()
    _task(queue_dir, "waits", depends_on=["never-finishes"])

    _tick(queue_dir, idle_log, snapshot=_snapshot(SupervisorState.THROTTLED_WEEKLY))

    assert _idle_lines(caplog) == []
    assert idle_log.last_breakdown is None


def test_silent_when_every_slot_is_taken(
    queue_dir: Path, caplog: pytest.LogCaptureFixture, no_spawn: None
) -> None:
    caplog.set_level(logging.INFO, logger=ORCHESTRATOR_LOGGER)
    idle_log = IdleDispatchLog()
    _task(queue_dir, "waits", depends_on=["never-finishes"])
    release = threading.Event()
    busy = threading.Thread(target=release.wait)
    busy.start()
    try:
        slots = {
            "busy": DispatchSlot(task_id="busy", account="default", started_at=NOW, thread=busy)
        }
        tick_dispatch(
            queue_dir=queue_dir,
            settings=_settings(max_c=1),
            clock=FakeClock(NOW),
            snapshot=_snapshot(),
            in_flight_slots=slots,
            accounts=_ACCOUNTS,
            idle_log=idle_log,
        )
    finally:
        release.set()
        busy.join(timeout=2)

    assert _idle_lines(caplog) == []
    assert idle_log.last_breakdown is None


# --- the supervisor keeps one IdleDispatchLog for its lifetime ----------------


def _reading() -> UsageReading:
    """Low use late in the week, so the account is open for dispatch."""
    return UsageReading(
        captured_at=NOW,
        five_hour=WindowReading(
            utilization_pct=1, resets_at_raw="x", resets_at=NOW + timedelta(hours=4)
        ),
        seven_day=WindowReading(
            utilization_pct=1, resets_at_raw="x", resets_at=NOW + timedelta(days=1)
        ),
    )


def test_supervisor_logs_a_drained_queue_once(
    queue_dir: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    private_global_lock: Path,
) -> None:
    """Three real supervisor ticks over a queue with nothing eligible log the
    summary once: the daemon passes the same IdleDispatchLog every tick."""
    caplog.set_level(logging.INFO, logger=ORCHESTRATOR_LOGGER)
    monkeypatch.setattr(daemon_mod, "sleep_for_next_poll", lambda **kw: None)
    _task(queue_dir, "waits", depends_on=["never-finishes"])

    start_daemon(
        queue_dir=queue_dir,
        settings=load_settings(None),
        source=FakeUsageSource([_reading()] * 3),
        pending_count_fn=lambda: 1,
        in_flight_count_fn=lambda: 0,
        clock=FakeClock(NOW),
        install_signal_handlers=False,
        max_ticks=3,
    )

    assert _idle_lines(caplog) == [
        (logging.INFO, "no eligible tasks: 1 unfinished held (depends_on 1)")
    ]
