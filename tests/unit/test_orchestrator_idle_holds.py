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
from datetime import timedelta
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
from claude_task_runner.queue.store import (
    queue_runtime_dir,
    state_path_for,
    task_path_for,
    todo_dir,
)
from claude_task_runner.runner import orchestrator as orchestrator_mod
from claude_task_runner.runner.in_flight import DispatchSlot
from claude_task_runner.runner.orchestrator import IdleDispatchLog, tick_dispatch
from claude_task_runner.supervisor import daemon as daemon_mod
from claude_task_runner.supervisor.daemon import start_daemon
from claude_task_runner.supervisor.states import (
    AccountState,
    InFlightRecord,
    SupervisorSnapshot,
    SupervisorState,
)
from claude_task_runner.usage.models import UsageReading, WindowReading
from claude_task_runner.usage.source import FakeUsageSource

from ._mixed_queue import (
    BLOCK_FILE,
    MIXED_EXPECTED_ELIGIBLE,
    MIXED_EXPECTED_MISSING,
    MIXED_EXPECTED_SKIPS,
    NOW,
    STALE_HOLDS,
    add_state,
    add_task,
    build_mixed_queue,
    forbid_selector_writes,
)
from ._sidecar_files import make_request, write_request

ORCHESTRATOR_LOGGER = "claude_task_runner.runner.orchestrator"

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
    build_mixed_queue(queue_dir, tmp_path / "absent" / "paper.md")
    skips: Counter[str] = Counter()

    assert _select(queue_dir, skips) == MIXED_EXPECTED_ELIGIBLE
    assert skips == MIXED_EXPECTED_SKIPS


def test_every_task_yaml_is_eligible_or_counted(queue_dir: Path, tmp_path: Path) -> None:
    """Returned tasks plus counted skips account for every YAML in todo/.

    A skip that records no category makes the sum fall short."""
    build_mixed_queue(queue_dir, tmp_path / "absent" / "paper.md")
    skips: Counter[str] = Counter()

    eligible = _select(queue_dir, skips)

    n_yamls = len(list(todo_dir(queue_dir).glob("*.yaml")))
    assert n_yamls == 23
    assert len(eligible) + sum(skips.values()) == n_yamls


def test_breakdown_is_stable_across_ticks(queue_dir: Path, tmp_path: Path) -> None:
    """The first pass parks held tasks as deferred; the second pass must
    still put each under the gate that holds it, not under the park."""
    build_mixed_queue(queue_dir, tmp_path / "absent" / "paper.md")
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
        build_mixed_queue(qd, missing)

    assert _select(without, None) == _select(with_skips, Counter())

    def states(qd: Path) -> dict[str, bytes]:
        return {p.name: p.read_bytes() for p in state_path_for(qd, "x").parent.glob("*.yaml")}

    assert states(without) == states(with_skips)
    assert set(states(without)) >= {"needs-file.yaml", "bad-effort.yaml"}


def _queue_copy(root: Path, missing: Path) -> Path:
    root.mkdir()
    queue_runtime_dir(root)
    todo_dir(root)
    build_mixed_queue(root, missing)
    (root / "claude_runner.toml").write_text(
        f'[dispatch]\ndispatch_block_file = "{BLOCK_FILE}"\n', encoding="utf-8"
    )
    return root


def _state_bytes(qd: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in state_path_for(qd, "x").parent.glob("*.yaml")}


def _in_flight_snapshot(task_id: str) -> SupervisorSnapshot:
    return SupervisorSnapshot(
        state=SupervisorState.DISPATCHING,
        since=NOW,
        in_flight=[InFlightRecord(task_id=task_id, account="default", started_at=NOW)],
    )


def test_preview_is_the_selector_without_its_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``preview_selection`` admits and counts exactly what a supervisor
    tick's selection does, and writes nothing: it calls no state writer, and
    the two stale holds stay parked. The tick's pass, on a second copy of
    the queue, parks the held tasks and un-parks the stale ones, so the
    byte comparison can tell."""
    missing = tmp_path / "absent" / "paper.md"
    previewed = _queue_copy(tmp_path / "previewed", missing)
    ticked = _queue_copy(tmp_path / "ticked", missing)
    before = _state_bytes(previewed)
    settings = load_settings(previewed / "claude_runner.toml")

    with monkeypatch.context() as patched:
        writes = forbid_selector_writes(patched)
        preview = orchestrator_mod.preview_selection(
            previewed, settings, snapshot=_in_flight_snapshot("inflight"), now=NOW
        )
    assert writes == []
    skips: Counter[str] = Counter()
    ticked_ids = sorted(
        t.id
        for t in _eligible_candidates(
            ticked,
            _in_flight("inflight"),
            {"blocked-done", "done"},
            now=NOW,
            block_file=BLOCK_FILE,
            skips=skips,
        )
    )

    assert [t.id for t in preview.eligible] == ticked_ids == MIXED_EXPECTED_ELIGIBLE
    assert preview.skips == skips == MIXED_EXPECTED_SKIPS
    assert preview.missing_dependencies == MIXED_EXPECTED_MISSING
    assert preview.eligible_statuses == {
        "cooldown-over": "deferred",
        "effort-fixed": "deferred",
        "file-arrived": "deferred",
        "ready": "new",
        "sidecar-answered": "awaiting_sidecar",
    }
    assert (preview.queue_cap, preview.warming_up) == (None, False)
    assert _state_bytes(previewed) == before
    after_tick = _state_bytes(ticked)
    for task_id in STALE_HOLDS:
        assert b"status: pending" in after_tick[f"{task_id}.yaml"]
        assert b"status: deferred" in before[f"{task_id}.yaml"]
    assert after_tick != before


def test_completed_whichever_check_skips_it(queue_dir: Path) -> None:
    """A completed task the block-list or the in-flight set skips first is
    still reported as completed, never as held."""
    add_task(queue_dir, "blocked-done")
    add_task(queue_dir, "inflight-done")
    for tid in ("blocked-done", "inflight-done"):
        add_state(queue_dir, tid, "completed")
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
    add_task(
        queue_dir, "a", requires=[{"kind": "file", "path": missing}, {"kind": "sidecar_response"}]
    )
    add_task(
        queue_dir, "b", requires=[{"kind": "sidecar_response"}, {"kind": "file", "path": missing}]
    )
    write_request(queue_dir, make_request("a"))
    write_request(queue_dir, make_request("b"))
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
    add_task(queue_dir, "waits", depends_on=["never-finishes"])
    add_state(queue_dir, "never-finishes", "pending")

    _tick(queue_dir, idle_log)
    assert _idle_lines(caplog) == [
        (logging.INFO, "no eligible tasks: 1 unfinished held (depends_on 1)")
    ]

    # Same breakdown: silent.
    _tick(queue_dir, idle_log)
    assert _idle_lines(caplog) == []

    # Breakdown changes: logged again.
    add_task(queue_dir, "later")
    add_state(queue_dir, "later", "deferred", next_eligible_at=NOW + timedelta(hours=1))
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
    add_task(queue_dir, "ready")
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


def test_a_missing_dependency_has_its_own_count_and_one_warning(
    queue_dir: Path, caplog: pytest.LogCaptureFixture, no_spawn: None
) -> None:
    """Through real ticks: the line counts a task whose dependency does not
    exist apart from one waiting for a task that does, and the WARNING that
    names the missing id is logged once, not once per tick."""
    caplog.set_level(logging.INFO, logger=ORCHESTRATOR_LOGGER)
    idle_log = IdleDispatchLog()
    add_task(queue_dir, "waits", depends_on=["never-finishes"])
    add_state(queue_dir, "never-finishes", "pending")
    add_task(queue_dir, "orphan", depends_on=["115-pouzin_2022_tusamitamab"])

    for _ in range(3):
        _tick(queue_dir, idle_log)

    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.name == ORCHESTRATOR_LOGGER and r.levelno == logging.WARNING
    ]
    assert warnings == [
        "parking task orphan as deferred: missing dependency: depends_on names "
        "115-pouzin_2022_tusamitamab, but no task in todo/ or state/ has that id. Fix the "
        "task's depends_on, or create the missing task in todo/"
    ]
    assert _idle_lines(caplog) == [
        (
            logging.INFO,
            "no eligible tasks: 2 unfinished held (depends_on 1, depends_on_missing 1)",
        )
    ]


def test_completed_tasks_change_nothing_in_the_line(
    queue_dir: Path, caplog: pytest.LogCaptureFixture, no_spawn: None
) -> None:
    caplog.set_level(logging.INFO, logger=ORCHESTRATOR_LOGGER)
    idle_log = IdleDispatchLog()
    add_task(queue_dir, "done-1")
    add_state(queue_dir, "done-1", "completed")

    _tick(queue_dir, idle_log)
    assert _idle_lines(caplog) == [
        (logging.INFO, "no eligible tasks: no unfinished tasks in todo/")
    ]

    # Another task completes: the unfinished breakdown is unchanged.
    add_task(queue_dir, "done-2")
    add_state(queue_dir, "done-2", "completed")
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
    add_task(queue_dir, "waits", depends_on=["never-finishes"])
    add_state(queue_dir, "never-finishes", "pending")

    _tick(queue_dir, idle_log, snapshot=_snapshot(SupervisorState.THROTTLED_WEEKLY))

    assert _idle_lines(caplog) == []
    assert idle_log.last_breakdown is None


def test_silent_when_every_slot_is_taken(
    queue_dir: Path, caplog: pytest.LogCaptureFixture, no_spawn: None
) -> None:
    caplog.set_level(logging.INFO, logger=ORCHESTRATOR_LOGGER)
    idle_log = IdleDispatchLog()
    add_task(queue_dir, "waits", depends_on=["never-finishes"])
    add_state(queue_dir, "never-finishes", "pending")
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
    add_task(queue_dir, "waits", depends_on=["never-finishes"])
    add_state(queue_dir, "never-finishes", "pending")

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
