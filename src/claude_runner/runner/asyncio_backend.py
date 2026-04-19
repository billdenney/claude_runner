"""asyncio backend — drives tasks through claude_agent_sdk.query()."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from claude_runner.defaults import EFFORT_TABLE
from claude_runner.models import DispatchResult, StopReason, TaskStatus, TokenUsage
from claude_runner.notify.emitter import EventEmitter
from claude_runner.runner.stop_hook import make_stop_hook
from claude_runner.state.store import StateStore
from claude_runner.todo.schema import TaskSpec

_log = logging.getLogger(__name__)


class AsyncioBackend:
    name = "asyncio"

    def __init__(self, *, state_store: StateStore, emitter: EventEmitter) -> None:
        self._state_store = state_store
        self._emitter = emitter

    async def run_task(self, spec: TaskSpec) -> DispatchResult:
        try:
            from claude_agent_sdk import (
                ClaudeAgentOptions,
                HookMatcher,
                query,
            )
        except ImportError as exc:
            raise RuntimeError(
                "claude-agent-sdk is not installed; install it or use backend=subprocess"
            ) from exc

        state = self._state_store.load(spec.id)
        state.status = TaskStatus.RUNNING
        state.attempts += 1
        state.last_started_at = datetime.now(tz=UTC)
        state.error = None
        self._state_store.save(state)

        stop_hook = make_stop_hook(
            task_id=spec.id,
            state_store=self._state_store,
            emitter=self._emitter,
        )

        tier = EFFORT_TABLE[spec.effort]
        options_kwargs: dict[str, Any] = {
            "cwd": str(spec.working_dir),
            "allowed_tools": list(spec.allowed_tools),
            "max_turns": spec.max_turns,
            "hooks": {"Stop": [HookMatcher(hooks=[stop_hook])]},  # type: ignore[list-item]
            "model": spec.model,
        }
        if state.session_id:
            options_kwargs["resume"] = state.session_id
        if tier.thinking_budget_tokens > 0:
            options_kwargs["extra_args"] = {
                "thinking": {"type": "enabled", "budget_tokens": tier.thinking_budget_tokens}
            }

        options = ClaudeAgentOptions(**options_kwargs)

        started = datetime.now(tz=UTC)
        usage = TokenUsage()
        stop_reason = StopReason.END_TURN
        error: str | None = None
        success = True

        try:
            async for message in query(prompt=spec.prompt, options=options):
                self._handle_message(spec.id, message, usage)
        except Exception as exc:
            success = False
            stop_reason = StopReason.ERROR
            error = f"{type(exc).__name__}: {exc}"
            _log.exception("task %s crashed", spec.id)

        finished = datetime.now(tz=UTC)
        duration_s = (finished - started).total_seconds()

        # The Stop hook usually updates state on success, but we also reconcile
        # here so an exception path still produces a correct state file.
        final = self._state_store.load(spec.id)
        if not success:
            final.status = TaskStatus.FAILED
            final.stop_reason = StopReason.ERROR
            final.error = error
            final.last_finished_at = finished
            self._state_store.save(final)
        else:
            stop_reason = final.stop_reason or StopReason.END_TURN

        return DispatchResult(
            task_id=spec.id,
            success=success and final.status == TaskStatus.COMPLETED,
            usage=usage,
            stop_reason=stop_reason,
            session_id=final.session_id,
            duration_s=duration_s,
            error=error,
        )

    def _handle_message(self, task_id: str, message: object, usage: TokenUsage) -> None:
        # The SDK returns typed messages; we introspect duck-typed to survive
        # minor version drift. Known shapes:
        #   SystemMessage(subtype='init', data={'session_id': ...})
        #   ResultMessage(usage={...}, total_cost_usd=..., subtype='success'|...)
        subtype = getattr(message, "subtype", None)
        data = getattr(message, "data", None)
        if subtype == "init" and isinstance(data, dict):
            sid = data.get("session_id")
            if isinstance(sid, str):
                self._state_store.write_session_id(task_id, sid)
                self._emitter.emit("session_captured", task_id=task_id, session_id=sid)
                return

        result_usage = getattr(message, "usage", None)
        if result_usage:
            _accumulate_usage(usage, result_usage)
        cost = getattr(message, "total_cost_usd", None)
        if isinstance(cost, (int, float)):
            usage.cost_usd = float(cost)


def _accumulate_usage(total: TokenUsage, payload: object) -> None:
    if hasattr(payload, "__dict__"):
        d = payload.__dict__
    elif isinstance(payload, dict):
        d = payload
    else:
        return
    total.input_tokens += int(d.get("input_tokens") or 0)
    total.output_tokens += int(d.get("output_tokens") or 0)
    total.cache_read_tokens += int(
        d.get("cache_read_input_tokens") or d.get("cache_read_tokens") or 0
    )
    total.cache_creation_tokens += int(
        d.get("cache_creation_input_tokens") or d.get("cache_creation_tokens") or 0
    )
