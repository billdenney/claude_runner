"""A queue with one task per way the dispatch selector can hold a task.

Shared by the selector's tests and the ``queue holds`` command's: the same
fixture must classify the same way through both.
"""

from __future__ import annotations

import functools
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn

import pytest

from claude_task_runner.queue import store as store_mod
from claude_task_runner.queue.schema import Task, TaskState
from claude_task_runner.queue.sidecar import write_response
from claude_task_runner.queue.store import (
    state_path_for,
    task_path_for,
    todo_dir,
    write_state_atomic,
    write_task_atomic,
)
from claude_task_runner.runner import orchestrator as orchestrator_mod

from ._sidecar_files import make_request, make_response, write_request

NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)
BLOCK_FILE = "needs_acquisition.jsonl"


def add_task(qd: Path, task_id: str, **fields: Any) -> Task:
    task = Task.model_validate(
        {"id": task_id, "title": f"Task {task_id}", "prompt": "do the thing", **fields}
    )
    write_task_atomic(task, task_path_for(qd, task_id))
    return task


def add_state(qd: Path, task_id: str, status: str, **fields: Any) -> None:
    state = TaskState.model_validate({"task_id": task_id, "status": status, **fields})
    write_state_atomic(state, state_path_for(qd, task_id))


def build_mixed_queue(qd: Path, missing_file: Path) -> None:
    """One task per way the selector can hold a task, plus completed and
    eligible ones, two of them (:data:`STALE_HOLDS`) still parked by a hold
    that has cleared. ``missing_file`` is an absolute path that does not exist,
    so two copies of this queue write identical readiness reasons. The task
    ``inflight`` is in flight only for a caller that says so. The cooldowns
    are a year either side of :data:`NOW`, the time every caller must select
    at: a caller selecting at the wall clock would see ``cooldown`` become
    eligible once ``NOW`` is a year past."""
    (todo_dir(qd) / "broken.yaml").write_text("not: valid: yaml: [\n", encoding="utf-8")

    # block_dispatch row; the second id's task is completed, so it counts
    # as completed rather than held.
    add_task(qd, "blocked")
    add_task(qd, "blocked-done")
    add_state(qd, "blocked-done", "completed")
    (qd / BLOCK_FILE).write_text(
        '{"task": "blocked", "block_dispatch": true}\n'
        '{"task": "blocked-done", "block_dispatch": true}\n',
        encoding="utf-8",
    )

    add_task(qd, "inflight")

    add_task(qd, "corrupt-state")
    state_path_for(qd, "corrupt-state").write_text("not yaml: ][[", encoding="utf-8")

    add_task(qd, "sidecar-open")
    add_state(qd, "sidecar-open", "awaiting_sidecar")
    write_request(qd, make_request("sidecar-open"))

    add_task(qd, "cooldown")
    add_state(
        qd,
        "cooldown",
        "deferred",
        deferred_reason="pre-dispatch hook deferred (exit 1): waiting",
        next_eligible_at=NOW + timedelta(days=365),
    )

    for status in ("running", "possibly_hung", "failed_circuit_breaker", "weekly_paused"):
        add_task(qd, f"status-{status}")
        add_state(qd, f"status-{status}", status)

    add_task(qd, "done")
    add_state(qd, "done", "completed")

    add_task(qd, "bad-effort", model="claude-sonnet-4-6", effort="max")

    # depends_on: a task that exists and has not completed. A dependency
    # that names no task at all is held apart, even beside an unfinished one.
    add_task(qd, "waits-on-upstream", depends_on=["status-running"])
    add_task(qd, "names-a-missing-task", depends_on=["status-running", "no-such-task"])

    add_task(qd, "needs-file", requires=[{"kind": "file", "path": str(missing_file)}])

    add_task(qd, "needs-answer", requires=[{"kind": "sidecar_response"}])
    write_request(qd, make_request("needs-answer"))

    add_task(
        qd,
        "needs-both",
        requires=[{"kind": "sidecar_response"}, {"kind": "file", "path": str(missing_file)}],
    )
    write_request(qd, make_request("needs-both"))

    # Eligible: no state, a deferral past its cooldown, an answered sidecar.
    add_task(qd, "ready")
    add_task(qd, "cooldown-over")
    add_state(
        qd,
        "cooldown-over",
        "deferred",
        deferred_reason="pre-dispatch hook deferred (exit 1): waiting",
        next_eligible_at=NOW - timedelta(days=365),
    )
    add_task(qd, "sidecar-answered")
    add_state(qd, "sidecar-answered", "awaiting_sidecar")
    write_request(qd, make_request("sidecar-answered"))
    write_response(qd, make_response("sidecar-answered"))

    # Eligible, and parked by a hold that has since cleared: the live
    # selector un-parks each (a state write); a read-only one must not.
    # Their reasons are the gates' own, without a cooldown.
    add_task(qd, "effort-fixed")
    add_state(
        qd,
        "effort-fixed",
        "deferred",
        deferred_reason="invalid effort: claude-sonnet-4-6 does not accept effort 'max'",
    )
    (qd / "arrived.md").write_text("here now\n", encoding="utf-8")
    add_task(qd, "file-arrived", requires=[{"kind": "file", "path": "arrived.md"}])
    add_state(
        qd,
        "file-arrived",
        "deferred",
        deferred_reason="readiness hold: file arrived.md does not exist",
    )


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
        "depends_on_missing": 1,
        "readiness_missing_file": 1,
        "readiness_awaiting_sidecar_response": 1,
        "readiness_awaiting_sidecar_response+missing_file": 1,
        "completed": 2,
    }
)
MIXED_EXPECTED_ELIGIBLE = [
    "cooldown-over",
    "effort-fixed",
    "file-arrived",
    "ready",
    "sidecar-answered",
]
MIXED_EXPECTED_MISSING = {"names-a-missing-task": ["no-such-task"]}
"""The ``depends_on_missing`` task and the dependency it names that no task has."""
STALE_HOLDS = ("effort-fixed", "file-arrived")
"""The eligible tasks whose state still carries a gate's cleared hold."""


SELECTOR_WRITERS = (
    "write_state_atomic",
    "state_path_for",
    "_park_deferred",
    "_unpark",
    "_clear_session_for_fresh_dispatch",
    "_park_live_worker",
)
"""Every name through which the selector writes a task's state or creates
its directory: the gates' park and un-park, the live-worker park, the
expired-affinity session clear, and the store functions they call."""


def _refuse(calls: list[str], name: str, *_args: object, **_kwargs: object) -> NoReturn:
    calls.append(name)
    raise AssertionError(f"a read-only selection called {name}")


def forbid_selector_writes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make each of :data:`SELECTOR_WRITERS`, and the store's state writer,
    record its name and raise. The selector swallows a failed write, so the
    returned list, not an exception, is what a read-only caller asserts
    stays empty."""
    calls: list[str] = []
    for name in SELECTOR_WRITERS:
        monkeypatch.setattr(orchestrator_mod, name, functools.partial(_refuse, calls, name))
    monkeypatch.setattr(
        store_mod,
        "write_state_atomic",
        functools.partial(_refuse, calls, "store.write_state_atomic"),
    )
    return calls
