"""Stop-hook helpers used by the Agent SDK backend.

The actual hook is registered per-task and closes over the task id + state
store + event emitter, so when the SDK fires Stop we can look up the right
task without inspecting the hook's session_id.

If the task exited ``end_turn`` AND left an open sidecar request, the hook
writes ``AWAITING_INPUT`` instead of ``COMPLETED`` so the scheduler knows
to pause the task until the operator answers.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from claude_runner.models import StopReason, TaskStatus
from claude_runner.notify.emitter import EventEmitter
from claude_runner.sidecar.store import SidecarStore
from claude_runner.state.store import StateStore

HookCallable = Callable[..., Awaitable[dict[str, Any]]]


def make_stop_hook(
    *,
    task_id: str,
    state_store: StateStore,
    emitter: EventEmitter,
    sidecar_store: SidecarStore | None = None,
) -> HookCallable:
    async def stop_hook(
        input_data: dict[str, Any] | None = None,
        tool_use_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = input_data or {}
        reason_raw = str(data.get("stop_reason") or "end_turn")
        try:
            stop_reason = StopReason(reason_raw)
        except ValueError:
            stop_reason = StopReason.END_TURN

        state = state_store.load(task_id)
        state.last_finished_at = datetime.now(tz=UTC)
        state.stop_reason = stop_reason

        awaiting_input = False
        open_request_seq: int | None = None
        if stop_reason == StopReason.END_TURN and sidecar_store is not None:
            open_req = sidecar_store.find_open_request(task_id)
            if open_req is not None:
                awaiting_input = True
                open_request_seq = open_req.sequence

        if awaiting_input:
            state.status = TaskStatus.AWAITING_INPUT
            state.error = None
        elif stop_reason == StopReason.END_TURN:
            state.status = TaskStatus.COMPLETED
            state.error = None
        else:
            state.status = TaskStatus.FAILED
            state.error = data.get("error") or f"stopped with reason={stop_reason.value}"
        state_store.save(state)

        if awaiting_input:
            emitter.emit(
                "task_awaiting_input",
                task_id=task_id,
                request_sequence=open_request_seq,
                session_id=state.session_id,
            )
        else:
            emitter.emit(
                "task_completed" if stop_reason == StopReason.END_TURN else "task_failed",
                task_id=task_id,
                stop_reason=stop_reason.value,
                session_id=state.session_id,
            )
        return {}

    return stop_hook
