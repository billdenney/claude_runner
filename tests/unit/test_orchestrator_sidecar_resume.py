"""Regression tests for the awaiting_sidecar -> dispatchable transition.

Before this fix the orchestrator's `_eligible_candidates` skipped any
task whose state showed `awaiting_sidecar`, with no path back to
re-dispatch even after the operator wrote a `response-NNN.json` file.
Tasks stayed stuck forever and the operator had to manually edit the
state YAML to unstick them.

The fix: when a task's state is `awaiting_sidecar`, check
`list_open_sidecars` — if NO sidecar request for that task is still
without a response, the task is eligible again. New requests filed
after a response (sequence 2+ without a response-002) keep the task
ineligible until the operator answers those too.
"""

from __future__ import annotations

import datetime as dt
import threading
from pathlib import Path

import pytest
import yaml

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
from claude_task_runner.queue.sidecar import write_request, write_response
from claude_task_runner.queue.store import (
    queue_runtime_dir,
    state_path_for,
    write_state_atomic,
)
from claude_task_runner.runner.in_flight import DispatchSlot
from claude_task_runner.runner.orchestrator import (
    _dispatch_blocked_task_ids,
    _eligible_candidates,
)


def _write_task_yaml(queue_dir: Path, task: Task) -> Path:
    """Persist a Task as todo/<id>.yaml. Mirrors what the operator does
    by hand. We don't import the runner's task-writer because tests
    elsewhere don't either; PyYAML's safe_dump is sufficient.
    """
    todo = queue_dir / "todo"
    todo.mkdir(parents=True, exist_ok=True)
    p = todo / f"{task.id}.yaml"
    p.write_text(
        yaml.safe_dump(task.model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    return p


def _make_task(task_id: str = "t1") -> Task:
    return Task(
        id=task_id,
        title=f"test task {task_id}",
        prompt="please do the thing",
    )


def _make_state_awaiting_sidecar(task_id: str) -> TaskState:
    return TaskState(
        task_id=task_id,
        status="awaiting_sidecar",
        attempts=1,
        last_started_at=dt.datetime(2026, 5, 15, 1, 0, 0, tzinfo=dt.UTC),
        last_finished_at=dt.datetime(2026, 5, 15, 1, 5, 0, tzinfo=dt.UTC),
    )


def _make_request(task_id: str, seq: int) -> SidecarRequest:
    return SidecarRequest(
        schema_version=CURRENT_SCHEMA_VERSION,
        task_id=task_id,
        sequence=seq,
        created_at=dt.datetime(2026, 5, 15, 1, 5, 0, tzinfo=dt.UTC),
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


def _make_response(task_id: str, seq: int) -> SidecarResponse:
    return SidecarResponse(
        schema_version=CURRENT_SCHEMA_VERSION,
        task_id=task_id,
        sequence=seq,
        responded_at=dt.datetime(2026, 5, 15, 12, 0, 0, tzinfo=dt.UTC),
        answers=[SidecarAnswer(id="q1", value="A")],
        notes="",
    )


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    # Materialise the runtime dir so write_request / state_path_for don't
    # need to fight with mkdir during the test.
    queue_runtime_dir(tmp_path).mkdir(parents=True, exist_ok=True)
    (tmp_path / "todo").mkdir(parents=True, exist_ok=True)
    return tmp_path


# --- Edge-case branches in `_eligible_candidates` -----------------------
#
# These tests cover branches that pre-date the awaiting_sidecar fix but
# had no direct coverage: unparseable YAML, in-flight skip, unparseable
# state, and unmet `depends_on`. They live in the same file because they
# share the same _eligible_candidates fixture surface — keeping them
# together avoids duplicate boilerplate.


def test_unparseable_task_yaml_is_skipped(queue_dir: Path) -> None:
    """A malformed task YAML must be skipped (logged warning), not raise."""
    (queue_dir / "todo" / "broken.yaml").write_text(
        "this is not: valid: yaml: at: all: [\n", encoding="utf-8"
    )
    in_flight: dict[str, DispatchSlot] = {}
    eligible = _eligible_candidates(queue_dir, in_flight, set())
    # The broken YAML produces no Task; eligible list is empty (or only
    # contains successfully-parsed tasks). Either way, no exception.
    assert all(t.id != "broken" for t in eligible)


def test_in_flight_task_is_skipped(queue_dir: Path) -> None:
    """A task whose id is in ``in_flight_threads`` must not be re-dispatched."""
    task = _make_task("t-running")
    _write_task_yaml(queue_dir, task)
    # No state file => normally eligible; but in-flight set masks it.
    sentinel_thread = threading.Thread(target=lambda: None)
    in_flight = {task.id: sentinel_thread}
    eligible = _eligible_candidates(queue_dir, in_flight, set())
    assert task.id not in {t.id for t in eligible}


def test_unparseable_state_file_is_skipped_not_dispatched(
    queue_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A state file that can't parse must cause the task to be SKIPPED, not
    silently treated as "not yet dispatched".

    Regression for the audit finding: the old ``except Exception: pass``
    fell through to the eligible list, so a *completed* task with a
    corrupt state file could be re-dispatched (duplicate work, wasted
    cap). The fix skips the task and logs the corruption at ERROR so an
    operator can locate and rebuild the bad file.
    """
    task = _make_task("t-corrupt-state")
    _write_task_yaml(queue_dir, task)
    state_path_for(queue_dir, task.id).parent.mkdir(parents=True, exist_ok=True)
    state_path_for(queue_dir, task.id).write_text("not yaml: ][[", encoding="utf-8")
    in_flight: dict[str, DispatchSlot] = {}
    with caplog.at_level("ERROR", logger="claude_task_runner.runner.orchestrator"):
        eligible = _eligible_candidates(queue_dir, in_flight, set())
    # Corrupt state ⇒ task skipped entirely.
    assert eligible == []
    # The corruption is surfaced at ERROR with the offending path.
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1
    assert task.id in errors[0].getMessage()
    assert str(state_path_for(queue_dir, task.id)) in errors[0].getMessage()


def test_unmet_depends_on_blocks_dispatch(queue_dir: Path) -> None:
    """A task whose depends_on includes a non-completed id is held back."""
    blocked = Task(
        id="blocked",
        title="blocked test task",
        prompt="please do the thing",
        depends_on=["upstream-not-done"],
    )
    _write_task_yaml(queue_dir, blocked)
    in_flight: dict[str, DispatchSlot] = {}
    eligible = _eligible_candidates(queue_dir, in_flight, set())
    assert "blocked" not in {t.id for t in eligible}

    # Once the upstream completes, blocked becomes eligible.
    eligible_after = _eligible_candidates(queue_dir, in_flight, {"upstream-not-done"})
    assert "blocked" in {t.id for t in eligible_after}


def test_awaiting_sidecar_with_open_request_is_not_eligible(
    queue_dir: Path,
) -> None:
    """Baseline: a task in awaiting_sidecar with an unanswered request
    is NOT picked up for dispatch — the operator must answer first."""
    task = _make_task("t1")
    _write_task_yaml(queue_dir, task)
    write_state_atomic(
        _make_state_awaiting_sidecar(task.id),
        state_path_for(queue_dir, task.id),
    )
    write_request(queue_dir, _make_request(task.id, 1))

    in_flight: dict[str, DispatchSlot] = {}
    eligible = _eligible_candidates(queue_dir, in_flight, set())
    assert eligible == []


def test_awaiting_sidecar_with_all_requests_answered_is_eligible(
    queue_dir: Path,
) -> None:
    """The bug fix: a task whose sole sidecar request has been answered
    becomes dispatchable again even though its state still says
    awaiting_sidecar. The dispatcher's next pass will overwrite the
    state when it spawns a new run."""
    task = _make_task("t1")
    _write_task_yaml(queue_dir, task)
    write_state_atomic(
        _make_state_awaiting_sidecar(task.id),
        state_path_for(queue_dir, task.id),
    )
    write_request(queue_dir, _make_request(task.id, 1))
    write_response(queue_dir, _make_response(task.id, 1))

    in_flight: dict[str, DispatchSlot] = {}
    eligible = _eligible_candidates(queue_dir, in_flight, set())
    assert [t.id for t in eligible] == [task.id]


def test_awaiting_sidecar_with_later_unanswered_request_is_not_eligible(
    queue_dir: Path,
) -> None:
    """Multi-round sidecar: request-001 answered, then the agent filed a
    follow-up request-002 that's still open. The task should NOT be
    eligible — operator must answer the new question too."""
    task = _make_task("t1")
    _write_task_yaml(queue_dir, task)
    write_state_atomic(
        _make_state_awaiting_sidecar(task.id),
        state_path_for(queue_dir, task.id),
    )
    write_request(queue_dir, _make_request(task.id, 1))
    write_response(queue_dir, _make_response(task.id, 1))
    write_request(queue_dir, _make_request(task.id, 2))

    in_flight: dict[str, DispatchSlot] = {}
    eligible = _eligible_candidates(queue_dir, in_flight, set())
    assert eligible == []


def test_awaiting_sidecar_with_all_multi_round_requests_answered_is_eligible(
    queue_dir: Path,
) -> None:
    """Multi-round sidecar with the LATEST request answered: eligible."""
    task = _make_task("t1")
    _write_task_yaml(queue_dir, task)
    write_state_atomic(
        _make_state_awaiting_sidecar(task.id),
        state_path_for(queue_dir, task.id),
    )
    write_request(queue_dir, _make_request(task.id, 1))
    write_response(queue_dir, _make_response(task.id, 1))
    write_request(queue_dir, _make_request(task.id, 2))
    write_response(queue_dir, _make_response(task.id, 2))

    in_flight: dict[str, DispatchSlot] = {}
    eligible = _eligible_candidates(queue_dir, in_flight, set())
    assert [t.id for t in eligible] == [task.id]


def test_other_terminal_statuses_still_skipped(queue_dir: Path) -> None:
    """The sidecar-resume escape hatch must NOT widen eligibility to
    other terminal statuses like `completed` or `failed_circuit_breaker`.
    Those stay skipped exactly as before."""
    for status, expect_eligible in [
        ("completed", False),
        ("failed_circuit_breaker", False),
        ("running", False),
        ("possibly_hung", False),
        # Plain `failed` IS in _DISPATCHABLE_STATUSES; sanity-check.
        ("failed", True),
        # `pending` IS in _DISPATCHABLE_STATUSES; sanity-check.
        ("pending", True),
    ]:
        # Fresh queue per status so no cross-contamination.
        sub = queue_dir / f"q-{status}"
        queue_runtime_dir(sub).mkdir(parents=True, exist_ok=True)
        (sub / "todo").mkdir(parents=True, exist_ok=True)
        task = _make_task(f"t-{status}")
        _write_task_yaml(sub, task)
        state = TaskState(
            task_id=task.id,
            status=status,  # type: ignore[arg-type]
            attempts=1,
        )
        write_state_atomic(state, state_path_for(sub, task.id))
        in_flight: dict[str, DispatchSlot] = {}
        eligible = _eligible_candidates(sub, in_flight, set())
        if expect_eligible:
            assert [t.id for t in eligible] == [task.id], (
                f"expected {task.id!r} eligible for status={status!r}"
            )
        else:
            assert eligible == [], f"expected nothing eligible for status={status!r}"


# --- `deferred` cooldown gating in `_eligible_candidates` ----------------
#
# A task parked by a pre-dispatch exit-1 deferral (input awaiting
# re-acquisition / trim) is re-dispatchable only once its
# `next_eligible_at` cooldown elapses. This is what lets a deferral stay
# OUT of the circuit breaker without being re-picked every tick — the
# starvation that originally forced hook failures to count toward it.

_NOW = dt.datetime(2026, 6, 21, 12, 0, 0, tzinfo=dt.UTC)


def _make_deferred_state(task_id: str, next_eligible_at: dt.datetime | None) -> TaskState:
    return TaskState(
        task_id=task_id,
        status="deferred",
        attempts=0,
        deferral_count=1,
        deferred_reason="DEFERRED: input awaits re-acquisition: /q/papers/PMID_X/PMID_X.pdf",
        next_eligible_at=next_eligible_at,
    )


def test_deferred_before_cooldown_is_skipped(queue_dir: Path) -> None:
    """Parked deferral whose cooldown has NOT elapsed: not re-dispatched."""
    task = _make_task("t-deferred-future")
    _write_task_yaml(queue_dir, task)
    write_state_atomic(
        _make_deferred_state(task.id, _NOW + dt.timedelta(minutes=10)),
        state_path_for(queue_dir, task.id),
    )
    eligible = _eligible_candidates(queue_dir, {}, set(), now=_NOW)
    assert task.id not in {t.id for t in eligible}


def test_deferred_after_cooldown_is_eligible(queue_dir: Path) -> None:
    """Once the cooldown elapses, the parked task is re-dispatchable — the
    re-dispatch re-runs the hook (which proceeds if the input is ready, or
    re-parks with a fresh cooldown)."""
    task = _make_task("t-deferred-past")
    _write_task_yaml(queue_dir, task)
    write_state_atomic(
        _make_deferred_state(task.id, _NOW - dt.timedelta(minutes=1)),
        state_path_for(queue_dir, task.id),
    )
    eligible = _eligible_candidates(queue_dir, {}, set(), now=_NOW)
    assert task.id in {t.id for t in eligible}


def test_deferred_without_next_eligible_at_is_eligible(queue_dir: Path) -> None:
    """A deferred state lacking a cooldown timestamp (legacy/edge) falls
    through as eligible so it isn't stranded."""
    task = _make_task("t-deferred-none")
    _write_task_yaml(queue_dir, task)
    write_state_atomic(
        _make_deferred_state(task.id, None),
        state_path_for(queue_dir, task.id),
    )
    eligible = _eligible_candidates(queue_dir, {}, set(), now=_NOW)
    assert task.id in {t.id for t in eligible}


# --- Dispatch block-list: `block_dispatch: true` selector skip (ADR-0029) --
#
# Part 2 of the slot-leak fix. An operator flags a known-blocked task
# (e.g. a paper awaiting a supplement) with `block_dispatch: true` in a
# queue-relative JSONL (the `needs_acquisition.jsonl` convention). The
# selector reads it and skips those tasks WITHOUT spending a
# dispatch+defer cycle each cooldown. The reader is fail-safe: a missing
# file / malformed line / non-matching row means "not blocked".


def _write_block_file(queue_dir: Path, name: str, lines: list[str]) -> str:
    (queue_dir / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return name


def test_dispatch_blocked_task_ids_off_when_unset(queue_dir: Path) -> None:
    """No configured block file → feature off → empty set (even if a file
    with that content would exist elsewhere)."""
    assert _dispatch_blocked_task_ids(queue_dir, None) == set()
    assert _dispatch_blocked_task_ids(queue_dir, "") == set()


def test_dispatch_blocked_task_ids_missing_file_is_empty(queue_dir: Path) -> None:
    """Configured but absent file → empty set (fail-safe, not an error)."""
    assert _dispatch_blocked_task_ids(queue_dir, "needs_acquisition.jsonl") == set()


def test_dispatch_blocked_task_ids_reads_only_flagged_task_rows(queue_dir: Path) -> None:
    """Mixed-content JSONL: only object rows with ``block_dispatch: true``
    AND a non-empty string ``task`` contribute. Everything else — an
    explicit ``false``, an index row, a ``target_path`` re-acquisition
    row, a flagged row missing ``task``, a malformed line, a blank line —
    is ignored."""
    name = _write_block_file(
        queue_dir,
        "needs_acquisition.jsonl",
        [
            '{"task": "211-goulooze", "block_dispatch": true, "needed": "suppl"}',
            '{"task": "400-metrum", "block_dispatch": true}',
            '{"task": "999-cleared", "block_dispatch": false}',
            '{"title": "some paper", "status": "INDEX_NEEDS_ACQUISITION"}',
            '{"target_path": "papers/PMID_X/PMID_X.pdf"}',
            '{"block_dispatch": true}',
            "not valid json at all",
            "",
        ],
    )
    assert _dispatch_blocked_task_ids(queue_dir, name) == {"211-goulooze", "400-metrum"}


def test_eligible_candidates_skips_blocked_pending_task(queue_dir: Path) -> None:
    """A pending task (normally eligible on its first attempt) is skipped
    when block-flagged — so the very first dispatch+defer is avoided —
    and remains eligible when the feature is off."""
    task = _make_task("t-blocked-pending")
    _write_task_yaml(queue_dir, task)
    name = _write_block_file(
        queue_dir,
        "needs_acquisition.jsonl",
        ['{"task": "t-blocked-pending", "block_dispatch": true}'],
    )

    with_block = _eligible_candidates(queue_dir, {}, set(), now=_NOW, block_file=name)
    assert task.id not in {t.id for t in with_block}

    without_block = _eligible_candidates(queue_dir, {}, set(), now=_NOW, block_file=None)
    assert task.id in {t.id for t in without_block}


def test_eligible_candidates_skips_blocked_deferred_task_past_cooldown(queue_dir: Path) -> None:
    """The churn case: a ``deferred`` task whose cooldown HAS elapsed is
    normally re-dispatchable (see test_deferred_after_cooldown_is_eligible),
    which is exactly the dispatch+defer loop the block-list short-circuits.
    With the task block-flagged, the selector drops it before dispatch."""
    task = _make_task("t-blocked-deferred")
    _write_task_yaml(queue_dir, task)
    write_state_atomic(
        _make_deferred_state(task.id, _NOW - dt.timedelta(minutes=1)),
        state_path_for(queue_dir, task.id),
    )
    name = _write_block_file(
        queue_dir,
        "needs_acquisition.jsonl",
        ['{"task": "t-blocked-deferred", "block_dispatch": true}'],
    )

    eligible = _eligible_candidates(queue_dir, {}, set(), now=_NOW, block_file=name)
    assert task.id not in {t.id for t in eligible}


def test_eligible_candidates_block_file_does_not_skip_other_tasks(queue_dir: Path) -> None:
    """A block entry for one task must not suppress an unrelated ready
    task sharing the same enumeration pass."""
    blocked = _make_task("t-blocked")
    ready = _make_task("t-ready")
    _write_task_yaml(queue_dir, blocked)
    _write_task_yaml(queue_dir, ready)
    name = _write_block_file(
        queue_dir,
        "needs_acquisition.jsonl",
        ['{"task": "t-blocked", "block_dispatch": true}'],
    )

    eligible = {t.id for t in _eligible_candidates(queue_dir, {}, set(), now=_NOW, block_file=name)}
    assert "t-ready" in eligible
    assert "t-blocked" not in eligible
