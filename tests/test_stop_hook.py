from __future__ import annotations

import json
from pathlib import Path

import pytest

from claude_runner.models import StopReason, TaskStatus
from claude_runner.notify.emitter import EventEmitter
from claude_runner.runner.stop_hook import make_stop_hook
from claude_runner.state.store import StateStore


@pytest.mark.asyncio
async def test_successful_stop_marks_completed(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    emitter = EventEmitter(
        events_path=store.events_path(), log_dir=store.root / "logs", stdout=False
    )
    hook = make_stop_hook(task_id="t", state_store=store, emitter=emitter)
    await hook(input_data={"stop_reason": "end_turn"})
    state = store.load("t")
    assert state.status is TaskStatus.COMPLETED
    assert state.stop_reason is StopReason.END_TURN


@pytest.mark.asyncio
async def test_error_stop_marks_failed(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    emitter = EventEmitter(
        events_path=store.events_path(), log_dir=store.root / "logs", stdout=False
    )
    hook = make_stop_hook(task_id="t", state_store=store, emitter=emitter)
    await hook(input_data={"stop_reason": "error", "error": "kaboom"})
    state = store.load("t")
    assert state.status is TaskStatus.FAILED
    assert state.error == "kaboom"


@pytest.mark.asyncio
async def test_emits_completion_event(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    emitter = EventEmitter(
        events_path=store.events_path(), log_dir=store.root / "logs", stdout=False
    )
    hook = make_stop_hook(task_id="t", state_store=store, emitter=emitter)
    await hook(input_data={"stop_reason": "end_turn"})
    lines = store.events_path().read_text().splitlines()
    assert any(json.loads(line)["event"] == "task_completed" for line in lines)


@pytest.mark.asyncio
async def test_unknown_stop_reason_defaults_to_end_turn(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    emitter = EventEmitter(
        events_path=store.events_path(), log_dir=store.root / "logs", stdout=False
    )
    hook = make_stop_hook(task_id="t", state_store=store, emitter=emitter)
    await hook(input_data={"stop_reason": "something_weird"})
    state = store.load("t")
    assert state.stop_reason is StopReason.END_TURN
