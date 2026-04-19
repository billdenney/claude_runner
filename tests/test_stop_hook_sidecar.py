"""Coverage for the AWAITING_INPUT branch in runner/stop_hook.py."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_runner.models import StopReason, TaskState, TaskStatus
from claude_runner.notify.emitter import EventEmitter
from claude_runner.runner.stop_hook import make_stop_hook
from claude_runner.sidecar.schema import (
    InteractionRequest,
    Option,
    Question,
    RequestState,
)
from claude_runner.sidecar.store import SidecarStore
from claude_runner.state.store import StateStore


def _open_request(sidecar: SidecarStore, *, task_id: str = "t1", seq: int = 1) -> None:
    req = InteractionRequest(
        task_id=task_id,
        sequence=seq,
        created_at=datetime(2026, 4, 19, 12, 0, tzinfo=UTC),
        summary="s",
        questions=[
            Question(id="q", prompt="p", options=[Option(value="A", label="A")]),
        ],
        state=RequestState.OPEN,
    )
    sidecar.write_request(req)


@pytest.mark.asyncio
async def test_stop_hook_transitions_to_awaiting_input_when_open_request_exists(
    tmp_path: Path,
) -> None:
    state_store = StateStore(tmp_path / ".state")
    state_store.save(TaskState(task_id="t1", status=TaskStatus.RUNNING))
    sidecar = SidecarStore(tmp_path / "sidecar")
    _open_request(sidecar)
    emitter = EventEmitter(
        events_path=state_store.events_path(),
        log_dir=state_store.root / "logs",
        stdout=False,
    )

    hook = make_stop_hook(
        task_id="t1",
        state_store=state_store,
        emitter=emitter,
        sidecar_store=sidecar,
    )
    await hook({"stop_reason": "end_turn"})

    reloaded = state_store.load("t1")
    assert reloaded.status is TaskStatus.AWAITING_INPUT
    assert reloaded.stop_reason is StopReason.END_TURN
    assert reloaded.error is None


@pytest.mark.asyncio
async def test_stop_hook_completes_when_no_open_request(
    tmp_path: Path,
) -> None:
    state_store = StateStore(tmp_path / ".state")
    state_store.save(TaskState(task_id="t1", status=TaskStatus.RUNNING))
    sidecar = SidecarStore(tmp_path / "sidecar")
    # no open request
    emitter = EventEmitter(
        events_path=state_store.events_path(),
        log_dir=state_store.root / "logs",
        stdout=False,
    )

    hook = make_stop_hook(
        task_id="t1",
        state_store=state_store,
        emitter=emitter,
        sidecar_store=sidecar,
    )
    await hook({"stop_reason": "end_turn"})

    reloaded = state_store.load("t1")
    assert reloaded.status is TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_stop_hook_fails_on_non_end_turn_stop_reason_even_with_open_request(
    tmp_path: Path,
) -> None:
    """A mid-turn interrupt should NOT put the task in AWAITING_INPUT."""
    state_store = StateStore(tmp_path / ".state")
    state_store.save(TaskState(task_id="t1", status=TaskStatus.RUNNING))
    sidecar = SidecarStore(tmp_path / "sidecar")
    _open_request(sidecar)
    emitter = EventEmitter(
        events_path=state_store.events_path(),
        log_dir=state_store.root / "logs",
        stdout=False,
    )

    hook = make_stop_hook(
        task_id="t1",
        state_store=state_store,
        emitter=emitter,
        sidecar_store=sidecar,
    )
    # max_turns is one of the non-END_TURN reasons.
    await hook({"stop_reason": "max_turns"})

    reloaded = state_store.load("t1")
    assert reloaded.status is TaskStatus.FAILED
    assert reloaded.error  # non-None


@pytest.mark.asyncio
async def test_stop_hook_coerces_unknown_stop_reason_to_end_turn(
    tmp_path: Path,
) -> None:
    """An unrecognized stop_reason string is coerced to END_TURN (and
    therefore transitions to COMPLETED when no sidecar request is open)."""
    state_store = StateStore(tmp_path / ".state")
    state_store.save(TaskState(task_id="t1", status=TaskStatus.RUNNING))
    emitter = EventEmitter(
        events_path=state_store.events_path(),
        log_dir=state_store.root / "logs",
        stdout=False,
    )

    hook = make_stop_hook(
        task_id="t1",
        state_store=state_store,
        emitter=emitter,
        sidecar_store=None,
    )
    await hook({"stop_reason": "not-a-real-reason"})

    reloaded = state_store.load("t1")
    assert reloaded.status is TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_stop_hook_uses_error_from_input_data_when_failing(tmp_path: Path) -> None:
    state_store = StateStore(tmp_path / ".state")
    state_store.save(TaskState(task_id="t1", status=TaskStatus.RUNNING))
    emitter = EventEmitter(
        events_path=state_store.events_path(),
        log_dir=state_store.root / "logs",
        stdout=False,
    )
    hook = make_stop_hook(
        task_id="t1",
        state_store=state_store,
        emitter=emitter,
        sidecar_store=None,
    )
    await hook({"stop_reason": "max_turns", "error": "boom"})
    reloaded = state_store.load("t1")
    assert reloaded.error == "boom"
