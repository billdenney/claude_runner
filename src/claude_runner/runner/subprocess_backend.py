"""Subprocess backend — drives tasks via `claude -p --output-format stream-json`."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import UTC, datetime

from claude_runner.models import DispatchResult, StopReason, TaskStatus, TokenUsage
from claude_runner.notify.emitter import EventEmitter
from claude_runner.state.store import StateStore
from claude_runner.todo.schema import TaskSpec

_log = logging.getLogger(__name__)


class SubprocessBackend:
    name = "subprocess"

    def __init__(
        self,
        *,
        state_store: StateStore,
        emitter: EventEmitter,
        binary: str = "claude",
    ) -> None:
        self._state_store = state_store
        self._emitter = emitter
        self._binary = binary

    async def run_task(self, spec: TaskSpec) -> DispatchResult:
        if shutil.which(self._binary) is None:
            raise RuntimeError(f"{self._binary!r} CLI not found on PATH")

        state = self._state_store.load(spec.id)
        state.status = TaskStatus.RUNNING
        state.attempts += 1
        state.last_started_at = datetime.now(tz=UTC)
        state.error = None
        self._state_store.save(state)

        args = [
            self._binary,
            "-p",
            spec.prompt,
            "--output-format",
            "stream-json",
            # `claude -p --output-format=stream-json` requires `--verbose`;
            # without it the CLI exits immediately with
            #   "When using --print, --output-format=stream-json requires --verbose"
            # (enforced since claude CLI 2.x).
            "--verbose",
            "--max-turns",
            str(spec.max_turns),
            "--allowedTools",
            ",".join(spec.allowed_tools),
            "--model",
            spec.model,
        ]
        if state.session_id:
            args.extend(["--resume", state.session_id])

        started = datetime.now(tz=UTC)
        usage = TokenUsage()
        stop_reason = StopReason.END_TURN
        error: str | None = None
        success = True

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(spec.working_dir),
            )
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                self._handle_line(spec.id, line.decode("utf-8", "replace").strip(), usage)
            stderr = (await proc.stderr.read()).decode("utf-8", "replace") if proc.stderr else ""
            rc = await proc.wait()
            if rc != 0:
                success = False
                stop_reason = StopReason.ERROR
                error = stderr.strip() or f"claude exited with code {rc}"
        except Exception as exc:
            success = False
            stop_reason = StopReason.ERROR
            error = f"{type(exc).__name__}: {exc}"
            _log.exception("subprocess task %s crashed", spec.id)

        finished = datetime.now(tz=UTC)
        duration_s = (finished - started).total_seconds()

        final = self._state_store.load(spec.id)
        if success:
            final.status = TaskStatus.COMPLETED
            final.stop_reason = StopReason.END_TURN
            final.error = None
        else:
            final.status = TaskStatus.FAILED
            final.stop_reason = stop_reason
            final.error = error
        final.last_finished_at = finished
        self._state_store.save(final)

        self._emitter.emit(
            "task_completed" if success else "task_failed",
            task_id=spec.id,
            stop_reason=stop_reason.value,
            session_id=final.session_id,
        )

        return DispatchResult(
            task_id=spec.id,
            success=success,
            usage=usage,
            stop_reason=stop_reason,
            session_id=final.session_id,
            duration_s=duration_s,
            error=error,
        )

    def _handle_line(self, task_id: str, line: str, usage: TokenUsage) -> None:
        if not line:
            return
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(msg, dict):
            return
        if msg.get("type") == "system" and msg.get("subtype") == "init":
            sid = msg.get("session_id")
            if isinstance(sid, str):
                self._state_store.write_session_id(task_id, sid)
                self._emitter.emit("session_captured", task_id=task_id, session_id=sid)
        result_usage = msg.get("usage")
        if isinstance(result_usage, dict):
            usage.input_tokens += int(result_usage.get("input_tokens") or 0)
            usage.output_tokens += int(result_usage.get("output_tokens") or 0)
            usage.cache_read_tokens += int(result_usage.get("cache_read_input_tokens") or 0)
            usage.cache_creation_tokens += int(result_usage.get("cache_creation_input_tokens") or 0)
        cost = msg.get("total_cost_usd")
        if isinstance(cost, (int, float)):
            usage.cost_usd = float(cost)
