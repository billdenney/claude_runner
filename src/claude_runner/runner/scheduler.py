"""Main scheduler loop — pulls from catalog, gates on budget, dispatches."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from claude_runner.budget.circuit_breaker import CircuitBreaker
from claude_runner.budget.controller import DecisionKind, TokenBudgetController
from claude_runner.config import Settings
from claude_runner.git.worktree import (
    WorktreeConfig,
    WorktreeError,
    setup_worktree,
    teardown_worktree,
)
from claude_runner.models import DispatchResult, RunRecord, StopReason, TaskStatus
from claude_runner.notify.emitter import EventEmitter
from claude_runner.runner.backend import RunnerBackend
from claude_runner.runner.preamble import build_preamble, should_inject
from claude_runner.sidecar.store import SidecarStore
from claude_runner.state.store import StateStore
from claude_runner.todo.catalog import CatalogEntry, TodoCatalog
from claude_runner.todo.schema import GitWorktreeSpec, TaskSpec

_log = logging.getLogger(__name__)


@dataclass(slots=True)
class SchedulerOutcome:
    completed: int
    failed: int
    breaker_tripped: bool
    breaker_reason: str | None


class Scheduler:
    def __init__(
        self,
        *,
        settings: Settings,
        catalog: TodoCatalog,
        backend: RunnerBackend,
        budget: TokenBudgetController,
        state_store: StateStore,
        emitter: EventEmitter,
        breaker: CircuitBreaker,
        sidecar_store: SidecarStore | None = None,
    ) -> None:
        self._settings = settings
        self._catalog = catalog
        self._backend = backend
        self._budget = budget
        self._state = state_store
        self._emitter = emitter
        self._breaker = breaker
        self._sidecar = sidecar_store
        self._in_flight: dict[str, asyncio.Task[DispatchResult]] = {}
        self._estimates: dict[str, int] = {}
        self._worktree_set_up: dict[str, GitWorktreeSpec] = {}
        self._reporting_interval_s = settings.reporting_interval_s
        self._report_page_offset = 0
        self._last_report_at: float = 0.0

    async def run(self) -> SchedulerOutcome:
        completed = 0
        failed = 0

        self._emitter.emit("run_started", backend=self._backend.name)
        try:
            while True:
                if self._breaker.tripped():
                    break
                self._budget.refresh()
                self._promote_ready_to_resume()
                self._emit_awaiting_snapshot_if_due()
                ready = self._catalog.ready_tasks(in_flight_ids=set(self._in_flight))
                awaiting_count = len(self._catalog.awaiting_input_tasks())
                if not ready and not self._in_flight and awaiting_count == 0:
                    self._emitter.emit("queue_drained")
                    break
                if not ready and not self._in_flight and awaiting_count > 0:
                    # Nothing to dispatch, but tasks are waiting on operator
                    # input — keep the reporting loop alive rather than exit.
                    await self._sleep_briefly(self._reporting_interval_s)
                    continue

                target = self._budget.target_concurrency()
                slots = max(target - len(self._in_flight), 0)

                launched_any = False
                wait_until: datetime | None = None
                stop_now = False

                for entry in ready[:slots]:
                    decision = self._budget.may_start(entry.spec.estimated_input_tokens)
                    if decision.kind is DecisionKind.OK:
                        self._launch(entry)
                        launched_any = True
                    elif decision.kind is DecisionKind.WAIT:
                        wait_until = decision.wait_until
                        self._emitter.emit(
                            "budget_wait",
                            reason=decision.reason,
                            wait_until=(wait_until.isoformat() if wait_until else None),
                        )
                        break
                    else:
                        self._emitter.emit("budget_stop", reason=decision.reason)
                        stop_now = True
                        break

                if stop_now:
                    break

                if not self._in_flight:
                    if wait_until is not None:
                        await self._sleep_until(wait_until)
                        continue
                    if (
                        not launched_any
                    ):  # pragma: no branch - launched_any True implies in_flight non-empty
                        # Nothing ran, nothing in flight, but catalog claims tasks exist.
                        # Most likely all remaining tasks are blocked on failed deps.
                        break

                done = await self._await_any()
                for result in done:
                    if result.success:
                        completed += 1
                        self._breaker.record_success()
                    else:
                        failed += 1
                        self._breaker.record_failure()
                        self._emitter.emit(
                            "task_failed_summary",
                            task_id=result.task_id,
                            error=result.error,
                        )
                self._catalog.invalidate()
        finally:
            # Gracefully finish anything still running.
            await self._drain()

        breaker_state = self._breaker.state()
        self._emitter.emit(
            "run_finished",
            completed=completed,
            failed=failed,
            breaker_tripped=breaker_state.tripped,
            breaker_reason=breaker_state.reason,
        )
        return SchedulerOutcome(
            completed=completed,
            failed=failed,
            breaker_tripped=breaker_state.tripped,
            breaker_reason=breaker_state.reason,
        )

    def _launch(self, entry: CatalogEntry) -> None:
        spec = entry.spec
        # Apply per-task worktree setup and/or resume-prompt mutation now
        # so the spec we pass to the backend is the final, ready-to-run one.
        spec = self._prepare_spec_for_dispatch(spec)

        estimate = spec.estimated_input_tokens
        self._estimates[spec.id] = estimate
        self._budget.reserve(estimate)

        state = self._state.load(spec.id)
        state.status = TaskStatus.QUEUED
        state.last_started_at = datetime.now(tz=UTC)
        self._state.save(state)

        self._emitter.emit("task_started", task_id=spec.id, effort=spec.effort.value)
        coro = self._run_one(spec)
        task = asyncio.create_task(coro, name=f"claude-runner:{spec.id}")
        self._in_flight[spec.id] = task

    def _prepare_spec_for_dispatch(self, spec: TaskSpec) -> TaskSpec:
        """Apply worktree setup and resume-prompt injection before dispatch.

        The original ``TaskSpec`` is frozen; we return a fresh instance with
        ``working_dir`` pointed at the worktree (if configured) and
        ``prompt`` prepended with the operator's sidecar response (if the
        task is resuming from AWAITING_INPUT).
        """
        override: dict[str, object] = {}

        # 1. Worktree setup (only when git_worktree block present).
        if spec.git_worktree is not None:
            gw = spec.git_worktree
            cfg = WorktreeConfig(
                repo=gw.repo,
                branch_name=gw.branch_name,
                branch_from=gw.branch_from,
                root=gw.root,
            )
            try:
                worktree_path = setup_worktree(
                    spec.id, cfg, default_root=self._settings.worktree_root
                )
            except WorktreeError as exc:
                _log.error("worktree setup failed for %s: %s", spec.id, exc)
                raise
            self._worktree_set_up[spec.id] = gw
            override["working_dir"] = worktree_path
            self._emitter.emit(
                "worktree_ready",
                task_id=spec.id,
                path=str(worktree_path),
                branch=gw.branch_name,
            )

        # 2. Resume-prompt injection when the task is resuming from
        # AWAITING_INPUT. Prepend the operator's answers to the prompt so
        # the resumed claude session sees them as the new user message.
        state = self._state.load(spec.id)
        if state.status is TaskStatus.READY_TO_RESUME and self._sidecar is not None:
            seqs = self._sidecar.list_request_sequences(spec.id)
            if seqs:
                seq = seqs[-1]
                try:
                    req = self._sidecar.load_request(spec.id, seq)
                    resp = self._sidecar.load_response(spec.id, seq)
                except Exception as exc:  # pragma: no cover - defensive
                    _log.warning(
                        "could not read sidecar for resume of %s seq=%d: %s",
                        spec.id,
                        seq,
                        exc,
                    )
                    req = None
                    resp = None
                if req is not None and resp is not None:
                    preamble = self._format_resume_preamble(req, resp)
                    override["prompt"] = preamble + "\n\n" + spec.prompt

        # 3. Inject runner preamble (sidecar protocol, worktree note,
        # gh-read-only rule) unless disabled per-task or via settings.
        if should_inject(spec=spec, settings_inject=self._settings.inject_preamble):
            sidecar_dir: Path | None = None
            if self._sidecar is not None:
                sidecar_dir = self._sidecar.task_dir(spec.id)
                sidecar_dir.mkdir(parents=True, exist_ok=True)
            preamble_wt: Path | None = None
            if spec.git_worktree:
                wt_candidate = override.get("working_dir")
                if isinstance(wt_candidate, Path):
                    preamble_wt = wt_candidate
            preamble_text = build_preamble(
                spec=spec,
                sidecar_dir=sidecar_dir,
                worktree_path=preamble_wt,
            )
            existing_prompt = override.get("prompt", spec.prompt)
            override["prompt"] = preamble_text + "\n\n" + str(existing_prompt)

        if not override:
            return spec
        return spec.model_copy(update=override)

    @staticmethod
    def _format_resume_preamble(req, resp) -> str:  # type: ignore[no-untyped-def]
        """Render a human-readable prompt preamble for a resumed task."""
        lines: list[str] = []
        lines.append(
            "You previously paused this task to request operator input. "
            "The operator has now answered. Use the answers below and "
            "continue the task from where you left off."
        )
        lines.append("")
        lines.append(f"Sidecar request sequence: {req.sequence}")
        lines.append(f"Summary: {req.summary}")
        lines.append("")
        lines.append("Answers:")
        q_by_id = {q.id: q for q in req.questions}
        for ans in resp.answers:
            q = q_by_id.get(ans.id)
            qtext = q.prompt if q is not None else ans.id
            lines.append(f"  - {ans.id} ({qtext}): {ans.value!r}")
        if resp.notes:
            lines.append("")
            lines.append(f"Operator notes: {resp.notes}")
        lines.append("")
        lines.append("(Original task brief follows.)")
        return "\n".join(lines)

    async def _run_one(self, spec: TaskSpec) -> DispatchResult:
        result = await self._backend.run_task(spec)
        estimate = self._estimates.pop(spec.id, spec.estimated_input_tokens)
        self._budget.release(estimate)
        self._budget.record_usage(result.usage, duration_s=result.duration_s)

        state = self._state.load(spec.id)
        state.runs.append(
            RunRecord(
                attempt=state.attempts or 1,
                started_at=state.last_started_at or datetime.now(tz=UTC),
                finished_at=state.last_finished_at or datetime.now(tz=UTC),
                usage=result.usage,
                stop_reason=result.stop_reason,
                error=result.error,
            )
        )
        self._state.save(state)

        # Tear down the worktree when the task reached a clean terminal state.
        # AWAITING_INPUT keeps the worktree so the resumed task finds it intact.
        final_status = state.status
        if final_status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            gw = self._worktree_set_up.pop(spec.id, None)
            if gw is not None:
                cfg = WorktreeConfig(
                    repo=gw.repo,
                    branch_name=gw.branch_name,
                    branch_from=gw.branch_from,
                    root=gw.root,
                )
                try:
                    removed = teardown_worktree(
                        spec.id, cfg, default_root=self._settings.worktree_root
                    )
                    if removed:
                        self._emitter.emit(
                            "worktree_removed",
                            task_id=spec.id,
                            branch=gw.branch_name,
                        )
                except WorktreeError as exc:
                    # Refusing-to-remove-dirty is expected when a task failed
                    # mid-work; we log and leave the worktree for debugging.
                    self._emitter.emit(
                        "worktree_retained",
                        task_id=spec.id,
                        branch=gw.branch_name,
                        reason=str(exc),
                    )
        return result

    async def _await_any(self) -> list[DispatchResult]:
        if not self._in_flight:
            return []
        done, _ = await asyncio.wait(
            list(self._in_flight.values()),
            return_when=asyncio.FIRST_COMPLETED,
        )
        results: list[DispatchResult] = []
        for task in done:
            tid = next((k for k, v in self._in_flight.items() if v is task), None)
            if tid is not None:  # pragma: no branch - tid is always found; defensive
                self._in_flight.pop(tid, None)
            try:
                results.append(task.result())
            except Exception as exc:
                _log.exception("backend raised for task %s", tid)
                if tid is not None:  # pragma: no branch - tid is always found; defensive
                    from claude_runner.models import TokenUsage

                    results.append(
                        DispatchResult(
                            task_id=tid,
                            success=False,
                            usage=TokenUsage(),
                            stop_reason=StopReason.ERROR,
                            session_id=None,
                            duration_s=0.0,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    )
        return results

    async def _sleep_until(self, when: datetime) -> None:
        now = datetime.now(tz=UTC)
        secs = max((when - now).total_seconds(), 1.0)
        self._emitter.emit("sleeping", seconds=int(secs), until=when.isoformat())
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._await_any_soft(), timeout=secs)

    async def _await_any_soft(self) -> None:
        if not self._in_flight:
            # Block long-ish; outer wait_for will cancel this.
            await asyncio.sleep(3600)
            return  # pragma: no cover - unreachable; sleep(3600) is always cancelled first
        await asyncio.wait(list(self._in_flight.values()), return_when=asyncio.FIRST_COMPLETED)

    async def _sleep_briefly(self, seconds: int) -> None:
        """Wait up to ``seconds`` for any in-flight task, or else sleep.

        Used when the only work remaining is ``AWAITING_INPUT`` tasks — we
        don't want to exit the scheduler (the operator might be about to
        answer) but we also don't want to busy-loop.
        """
        secs = max(int(seconds), 1)
        if self._in_flight:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._await_any_soft(), timeout=float(secs))
        else:
            await asyncio.sleep(float(secs))

    def _promote_ready_to_resume(self) -> None:
        """Scan AWAITING_INPUT tasks for a matching response; promote to READY_TO_RESUME.

        The promotion also mutates the task's prompt-on-resume: when the
        backend next dispatches the task it will include the operator's
        answers at the top of the new user message.
        """
        if self._sidecar is None:
            return
        for entry in self._catalog.awaiting_input_tasks():
            task_id = entry.spec.id
            seqs = self._sidecar.list_request_sequences(task_id)
            if not seqs:
                continue
            latest = seqs[-1]
            resp = None
            try:
                resp = self._sidecar.load_response(task_id, latest)
            except Exception as exc:  # pragma: no cover - defensive
                _log.warning(
                    "failed to load sidecar response for %s seq=%d: %s",
                    task_id,
                    latest,
                    exc,
                )
                continue
            if resp is None:
                continue
            if resp.state.value != "answered":
                continue
            state = self._state.load(task_id)
            if state.status is TaskStatus.AWAITING_INPUT:
                state.status = TaskStatus.READY_TO_RESUME
                self._state.save(state)
                self._emitter.emit(
                    "task_ready_to_resume",
                    task_id=task_id,
                    response_sequence=latest,
                    session_id=state.session_id,
                )
        # Invalidate catalog so the next ready_tasks() returns the promoted rows.
        self._catalog.invalidate()

    def _emit_awaiting_snapshot_if_due(self) -> None:
        """Emit an awaiting_input_snapshot event + write status_snapshot.json.

        Runs at most once per ``reporting_interval_s``. When the number of
        awaiting tasks exceeds ``report_max_per_tick``, emit a rotating page
        so every task is visible at least once per ceil(N/page) ticks; the
        snapshot file always contains the full list.

        Adaptive rate-limit: if a single emit takes longer than half the
        interval, double the interval and log a ``report_interval_adjusted``
        event so operators know polling slowed down.
        """
        if self._sidecar is None:
            return
        now = time.monotonic()
        if self._last_report_at and (now - self._last_report_at) < self._reporting_interval_s:
            return
        self._last_report_at = now

        start = time.monotonic()
        awaiting = self._catalog.awaiting_input_tasks()
        running_count = sum(1 for e in self._catalog.all_entries() if e.state.is_in_flight())
        pending_count = len(self._catalog.ready_tasks())

        # Build paginated "events" list, but the snapshot file lists all.
        page_size = max(int(self._settings.report_max_per_tick), 1)
        total = len(awaiting)
        if total == 0:
            self._report_page_offset = 0
            event_entries: list[CatalogEntry] = []
        else:
            start_idx = self._report_page_offset % total
            end_idx = min(start_idx + page_size, total)
            event_entries = awaiting[start_idx:end_idx]
            self._report_page_offset = end_idx if end_idx < total else 0

        def _describe(entry: CatalogEntry) -> dict[str, object]:
            assert self._sidecar is not None
            task_id = entry.spec.id
            seqs = self._sidecar.list_request_sequences(task_id)
            latest_seq = seqs[-1] if seqs else None
            summary: str | None = None
            created_at: str | None = None
            if latest_seq is not None:
                try:
                    req = self._sidecar.load_request(task_id, latest_seq)
                    summary = req.summary or None
                    created_at = req.created_at.isoformat()
                except Exception:  # pragma: no cover - defensive
                    summary = None
            age_seconds: float | None = None
            if entry.state.last_finished_at is not None:
                age_seconds = (datetime.now(tz=UTC) - entry.state.last_finished_at).total_seconds()
            return {
                "task_id": task_id,
                "sequence": latest_seq,
                "summary": summary,
                "created_at": created_at,
                "age_seconds": age_seconds,
            }

        self._emitter.emit(
            "awaiting_input_snapshot",
            running=running_count,
            pending=pending_count,
            awaiting_total=total,
            awaiting_page=[_describe(e) for e in event_entries],
        )

        # Write the always-complete snapshot file atomically.
        snapshot_path = self._state.root / "status_snapshot.json"
        payload = {
            "generated_at": datetime.now(tz=UTC).isoformat(),
            "running": running_count,
            "pending": pending_count,
            "awaiting_total": total,
            "awaiting": [_describe(e) for e in awaiting],
        }
        self._atomic_write_snapshot(snapshot_path, payload)

        elapsed = time.monotonic() - start
        if self._reporting_interval_s >= 2 and elapsed > self._reporting_interval_s / 2:
            new_interval = self._reporting_interval_s * 2
            self._emitter.emit(
                "report_interval_adjusted",
                old_s=self._reporting_interval_s,
                new_s=new_interval,
                reason=f"emit took {elapsed:.1f}s",
            )
            self._reporting_interval_s = new_interval
        elif (
            self._reporting_interval_s > self._settings.reporting_interval_s
            and elapsed < self._reporting_interval_s / 4
        ):
            # Back off the back-off when things are easy again.
            new_interval = max(self._reporting_interval_s // 2, self._settings.reporting_interval_s)
            if new_interval != self._reporting_interval_s:
                self._emitter.emit(
                    "report_interval_adjusted",
                    old_s=self._reporting_interval_s,
                    new_s=new_interval,
                    reason=f"recovered (emit took {elapsed:.1f}s)",
                )
                self._reporting_interval_s = new_interval

    @staticmethod
    def _atomic_write_snapshot(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2, sort_keys=False, default=str)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            finally:
                raise

    async def _drain(self) -> None:
        if not self._in_flight:
            return
        _log.info("draining %d in-flight task(s)", len(self._in_flight))
        await asyncio.gather(*self._in_flight.values(), return_exceptions=True)
        self._in_flight.clear()
