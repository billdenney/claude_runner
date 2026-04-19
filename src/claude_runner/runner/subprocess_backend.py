"""Subprocess backend — drives tasks via `claude -p --output-format stream-json`."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from datetime import UTC, datetime

from claude_runner.models import DispatchResult, StopReason, TaskStatus, TokenUsage
from claude_runner.notify.emitter import EventEmitter
from claude_runner.sidecar.store import SidecarStore
from claude_runner.state.store import StateStore
from claude_runner.todo.schema import TaskSpec

_log = logging.getLogger(__name__)

# Maximum stream-json line length (bytes) that the backend will accept from
# the claude CLI. The asyncio StreamReader default is 64 KiB, but a single
# stream-json message — especially one containing a large tool output, long
# extended-thinking block, or verbose debug payload — can easily exceed that
# and make ``readline()`` raise ``ValueError: Separator is found, but chunk is
# longer than limit``. Real-world messages seen in the wild have topped 1 MB;
# 16 MiB gives generous headroom without a practical memory cost.
_STREAM_READ_LIMIT = 16 * 1024 * 1024  # 16 MiB


class SubprocessBackend:
    name = "subprocess"

    def __init__(
        self,
        *,
        state_store: StateStore,
        emitter: EventEmitter,
        binary: str = "claude",
        sidecar_store: SidecarStore | None = None,
    ) -> None:
        self._state_store = state_store
        self._emitter = emitter
        self._binary = binary
        self._sidecar_store = sidecar_store

    async def run_task(self, spec: TaskSpec) -> DispatchResult:
        if shutil.which(self._binary) is None:
            raise RuntimeError(f"{self._binary!r} CLI not found on PATH")

        state = self._state_store.load(spec.id)
        state.status = TaskStatus.RUNNING
        state.attempts += 1
        state.last_started_at = datetime.now(tz=UTC)
        state.error = None
        self._state_store.save(state)

        # IMPORTANT: the prompt is piped via stdin rather than passed as an
        # argv. Passing the prompt as an argv means its entire text shows up
        # in ``ps aux`` / ``/proc/<pid>/cmdline``, which lets sub-agents
        # accidentally SIGTERM themselves with ``pkill -f <keyword>`` if the
        # prompt mentions <keyword>. (Observed in the wild: task 003 ran
        # ``pkill -f Xu_2019_sarilumab.Rmd`` to clean up a stuck R render,
        # matched its own parent claude process whose argv contained that
        # filename, and SIGTERMed itself 95% through the task.) Piping via
        # stdin removes the prompt text from cmdline entirely.
        args = [
            self._binary,
            "-p",
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

        # Expose sidecar + task_id to the child claude process so the skill
        # can write request-<seq>.json files back to us.
        env = os.environ.copy()
        env["CLAUDE_RUNNER_TASK_ID"] = spec.id
        if self._sidecar_store is not None:
            env["CLAUDE_RUNNER_SIDECAR_DIR"] = str(self._sidecar_store.task_dir(spec.id))

        try:
            # claude CLI emits newline-delimited JSON on stdout. A single
            # stream-json message can easily exceed asyncio's default 64 KiB
            # readline limit (large tool output, long extended-thinking
            # block, verbose debug payload), which otherwise raises
            # ``ValueError: Separator is found, but chunk is longer than
            # limit`` mid-stream and fails the whole task. Raise the reader
            # limit to 16 MiB — see _STREAM_READ_LIMIT.
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(spec.working_dir),
                env=env,
                limit=_STREAM_READ_LIMIT,
            )
            assert proc.stdout is not None
            assert proc.stdin is not None
            # Write the prompt to stdin and close it. See the note above
            # about why we don't pass it as an argv.
            proc.stdin.write(spec.prompt.encode("utf-8"))
            await proc.stdin.drain()
            proc.stdin.close()
            await proc.stdin.wait_closed()
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
        awaiting_input = False
        open_request_seq: int | None = None
        if success and self._sidecar_store is not None:
            open_req = self._sidecar_store.find_open_request(spec.id)
            if open_req is not None:
                awaiting_input = True
                open_request_seq = open_req.sequence

        if awaiting_input:
            final.status = TaskStatus.AWAITING_INPUT
            final.stop_reason = StopReason.END_TURN
            final.error = None
        elif success:
            final.status = TaskStatus.COMPLETED
            final.stop_reason = StopReason.END_TURN
            final.error = None
        else:
            final.status = TaskStatus.FAILED
            final.stop_reason = stop_reason
            final.error = error
        final.last_finished_at = finished
        self._state_store.save(final)

        if awaiting_input:
            self._emitter.emit(
                "task_awaiting_input",
                task_id=spec.id,
                request_sequence=open_request_seq,
                session_id=final.session_id,
            )
        else:
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
