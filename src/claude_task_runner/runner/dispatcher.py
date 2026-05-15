"""Dispatch a task as a ``claude`` subprocess and stream its output.

This is the top of the runner stack. It composes:

* :mod:`runner.session` to decide RESUME vs. FRESH spawn strategy.
* :mod:`runner.stream` to parse the NDJSON event stream.
* :mod:`runner.caps` to enforce per-task token/duration ceilings.
* :mod:`runner.heartbeat` to surface silent / hung subprocesses.
* :mod:`runner.hooks` to run pre/post-dispatch shell commands.
* :mod:`queue.store` to persist :class:`TaskState` updates atomically.

Subprocess lifecycle:

1. ``run_pre_dispatch`` — abort on non-zero exit.
2. Build argv via :func:`build_argv` (uses :class:`SpawnPlan`).
3. ``Popen`` with ``stdout=PIPE`` and line-buffered text mode.
4. Each line is parsed by :func:`parse_lines`; events update
   cumulative usage and last-heartbeat time.
5. After every event, evaluate :func:`runner.caps.evaluate_caps` and
   :func:`runner.heartbeat.evaluate`. SIGTERM on cap breach or kill-level
   silence.
6. On EOF, build a :class:`RunRecord` from the final ResultEvent.
7. ``run_post_dispatch`` (warning on failure, never task-failing).

The function is **synchronous and blocking** — one subprocess at a
time. The supervisor calls :func:`dispatch` in a thread (or
ProcessPoolExecutor) when ``target_concurrency > 1``.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from claude_task_runner.clock import Clock
from claude_task_runner.config.schema import (
    FailureClassifierSettings,
    HookSettings,
    SessionSettings,
    TaskCapsSettings,
)
from claude_task_runner.queue.schema import (
    RunRecord,
    Task,
    TaskState,
    TokenUsage,
)
from claude_task_runner.queue.sidecar import list_open_sidecars
from claude_task_runner.queue.store import (
    state_path_for,
    write_state_atomic,
)
from claude_task_runner.runner import caps as caps_mod
from claude_task_runner.runner import heartbeat as hb_mod
from claude_task_runner.runner import hooks as hooks_mod
from claude_task_runner.runner import retry as retry_mod
from claude_task_runner.runner.session import (
    ResumeStrategy,
    SpawnPlan,
    fall_through_to_fresh,
)
from claude_task_runner.runner.stream import (
    AssistantMessageEvent,
    ResultEvent,
    StreamSummary,
    SystemInitEvent,
    parse_lines,
)

logger = logging.getLogger(__name__)


class DispatchError(RuntimeError):
    """Dispatcher could not produce a usable RunRecord."""


@dataclass(frozen=True)
class DispatchOutcome:
    """Result of one ``dispatch()`` invocation.

    Attributes
    ----------
    run_record
        The :class:`RunRecord` to append to the task's state.
    new_state
        The updated :class:`TaskState`. Already persisted by ``dispatch``;
        returned so the supervisor can react in-memory.
    summary
        Aggregated stream-json totals (session id, cumulative usage,
        final result event).
    """

    run_record: RunRecord
    new_state: TaskState
    summary: StreamSummary


def build_argv(
    task: Task,
    plan: SpawnPlan,
    *,
    claude_executable: str = "claude",
) -> list[str]:
    """Build the argv for ``subprocess.Popen``.

    Always emits ``--print --output-format=stream-json --verbose`` (the
    combination that produces the NDJSON we parse). The ``--verbose``
    flag is required by the claude CLI when ``--print`` is paired with
    stream-json output.
    """
    argv: list[str] = [
        claude_executable,
        "--print",
        "--output-format=stream-json",
        "--verbose",
    ]
    if task.model:
        argv.extend(["--model", task.model])
    if task.allowed_tools:
        argv.extend(["--allowedTools", ",".join(task.allowed_tools)])
    if plan.strategy is ResumeStrategy.RESUME and plan.session_id:
        argv.extend(["--resume", plan.session_id])
    argv.extend(plan.extra_args)
    # The "--" separator is required: Claude's CLI parses --allowedTools as
    # variadic (<tools...>), so without an explicit terminator it greedily
    # consumes the trailing positional prompt as another tool name and the
    # subprocess errors out with "Input must be provided either through stdin
    # or as a prompt argument when using --print".
    argv.append("--")
    argv.append(plan.prompt)
    return argv


def _read_lines(stream: Iterable[str]) -> Iterable[str]:
    """Trivial pass-through; isolated for monkey-patching in tests."""
    yield from stream


def _build_run_record(
    *,
    attempt: int,
    started_at: datetime,
    finished_at: datetime,
    plan: SpawnPlan,
    summary: StreamSummary,
    cap_violation: caps_mod.CapViolation | None,
    process_exit_code: int,
    stderr_tail: str,
) -> RunRecord:
    """Assemble a RunRecord from the dispatch loop's accumulated state."""
    duration_s = (finished_at - started_at).total_seconds()

    final = summary.final_result
    if cap_violation is not None:
        stop_reason = "killed_by_cap"
        error: str | None = (
            f"{cap_violation.which} cap exceeded: "
            f"{cap_violation.observed:.0f} > {cap_violation.cap:.0f}"
        )
        usage = summary.cumulative_usage
        killed = cap_violation.which
    elif final is not None:
        stop_reason = final.stop_reason
        error = stderr_tail.strip() if final.is_error else None
        # Final result usage is authoritative; cumulative is a
        # secondary metric for in-flight tracking.
        usage = final.final_usage if final.final_usage.total_tokens else summary.cumulative_usage
        killed = None
    else:
        stop_reason = "process_exit_nonzero" if process_exit_code != 0 else "no_result"
        error = stderr_tail.strip() or (
            f"claude exited with code {process_exit_code} and no result event"
        )
        usage = summary.cumulative_usage
        killed = None

    cost = final.cost_usd if final is not None else 0.0

    return RunRecord(
        attempt=attempt,
        started_at=started_at,
        finished_at=finished_at,
        stop_reason=stop_reason,
        error=error,
        usage=usage,
        cost_usd=max(float(cost), 0.0),
        duration_s=duration_s,
        resumed_from_session=plan.session_id if plan.strategy is ResumeStrategy.RESUME else None,
        killed_by_cap=killed,
    )


def _dispatch_loop(
    *,
    process: subprocess.Popen[str],
    settings_caps: TaskCapsSettings,
    clock: Clock,
    task: Task,
    started_at: datetime,
) -> tuple[StreamSummary, caps_mod.CapViolation | None]:
    """Run the consume-and-monitor loop until the process exits or we kill it.

    Returns the StreamSummary and any cap violation observed.
    """
    summary = StreamSummary()
    cap_violation: caps_mod.CapViolation | None = None
    last_heartbeat = None

    assert process.stdout is not None
    for event in parse_lines(_read_lines(process.stdout), summary=summary):
        last_heartbeat = clock.now()
        if isinstance(event, (SystemInitEvent, AssistantMessageEvent, ResultEvent)):
            # Update last_heartbeat_at for every typed event.
            pass

        # After each event, check caps.
        cap_violation = caps_mod.evaluate_caps(
            settings=settings_caps,
            task=task,
            cumulative_tokens=summary.cumulative_usage.total_tokens,
            started_at=started_at,
            now=clock.now(),
        )
        if cap_violation is not None:
            logger.warning(
                "task %s exceeded %s cap (%.0f > %.0f); SIGTERM",
                task.id,
                cap_violation.which,
                cap_violation.observed,
                cap_violation.cap,
            )
            _terminate(process)
            break

        # Heartbeat-based kill (silence threshold). Alert-level transitions
        # are surfaced by the supervisor by comparing TaskState.last_heartbeat_at;
        # only a KILL verdict acts here.
        status = hb_mod.evaluate(
            settings=settings_caps,
            last_heartbeat_at=last_heartbeat,
            started_at=started_at,
            now=clock.now(),
        )
        if status.verdict is hb_mod.HeartbeatVerdict.KILL:
            logger.warning("task %s heartbeat silent for %.0fs; SIGTERM", task.id, status.silence_s)
            _terminate(process)
            cap_violation = caps_mod.CapViolation(
                which="duration",
                observed=status.silence_s,
                cap=float(settings_caps.heartbeat_silence_kill_s),
            )
            break

    return summary, cap_violation


def _terminate(process: subprocess.Popen[str]) -> None:
    """Try SIGTERM, escalate to SIGKILL after grace period."""
    try:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
    except OSError:
        pass


def dispatch(
    *,
    task: Task,
    state: TaskState,
    plan: SpawnPlan,
    queue_dir: Path,
    clock: Clock,
    settings_caps: TaskCapsSettings,
    settings_session: SessionSettings,
    settings_hooks: HookSettings,
    settings_failure_classifier: FailureClassifierSettings | None = None,
    claude_executable: str = "claude",
    claude_config_dir: str = "",
    persist_state: bool = True,
) -> DispatchOutcome:
    """Run one attempt for ``task`` and return the resulting state delta.

    Side effects (when ``persist_state=True``, the default):

    * Writes pre-run state with ``status="running"``.
    * Writes post-run state with appended :class:`RunRecord`, updated
      ``status``, and refreshed ``session_id``.

    The supervisor wraps this call with the throttling / concurrency
    decisions from :mod:`runner.concurrency`.
    """
    if shutil.which(claude_executable) is None:
        raise DispatchError(f"claude binary not found: {claude_executable}")

    # Pre-dispatch hook. Run with cwd=queue_dir (or None), NOT
    # task.working_dir: the hook's job is often to *create*
    # task.working_dir (e.g. `git worktree add`), so requiring it to
    # already exist would be a chicken-and-egg failure. The hook reads
    # $TASK_WORKING_DIR from its env (see HookEnv) when it needs to know
    # the target path.
    pre_hook_cwd = queue_dir if queue_dir.exists() else None
    hook_result = hooks_mod.run_pre_dispatch(
        settings_hooks,
        task,
        attempt=state.attempts + 1,
        session_id=state.session_id,
        cwd=pre_hook_cwd,
    )
    if hook_result is not None and (hook_result.timed_out or hook_result.exit_code != 0):
        # Pre-dispatch hook failure aborts dispatch with an error RunRecord.
        return _record_pre_dispatch_failure(
            task=task,
            state=state,
            plan=plan,
            queue_dir=queue_dir,
            hook_result=hook_result,
            clock=clock,
            persist_state=persist_state,
            settings_failure_classifier=settings_failure_classifier,
        )

    started_at = clock.now()
    new_state = state.model_copy(
        update={
            "status": "running",
            "attempts": state.attempts + 1,
            "last_started_at": started_at,
        }
    )
    if persist_state:
        write_state_atomic(new_state, state_path_for(queue_dir, task.id))

    argv = build_argv(task, plan, claude_executable=claude_executable)
    logger.info("dispatching task %s (attempt %d): %s", task.id, new_state.attempts, argv[0:1])

    # Build the subprocess env. When `claude_config_dir` is set (per-queue
    # config selects a non-default Claude account, e.g. ~/.claude_personal),
    # propagate it via CLAUDE_CONFIG_DIR so the dispatched `claude --print`
    # subprocess reads the same credentials the supervisor's /usage capture
    # uses. Without this, the supervisor sees personal-account utilization
    # while every dispatched task hits the default ~/.claude account --
    # which may be at a different / depleted quota.
    spawn_env: dict[str, str] | None = None
    if claude_config_dir:
        config_path = Path(claude_config_dir).expanduser()
        if not config_path.exists():
            raise DispatchError(f"CLAUDE_CONFIG_DIR does not exist: {config_path}")
        spawn_env = {**os.environ, "CLAUDE_CONFIG_DIR": str(config_path)}

    process = subprocess.Popen(  # caller-controlled
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        cwd=str(task.working_dir) if task.working_dir else None,
        env=spawn_env,
    )

    summary, cap_violation = _dispatch_loop(
        process=process,
        settings_caps=settings_caps,
        clock=clock,
        task=task,
        started_at=started_at,
    )

    # Drain remaining output and stderr.
    try:
        stdout_remainder, stderr = process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout_remainder, stderr = process.communicate()

    # Re-parse any remainder lines.
    if stdout_remainder:
        for _ in parse_lines(stdout_remainder.splitlines(), summary=summary):
            pass

    finished_at = clock.now()
    stderr_tail = (stderr or "")[-500:] if stderr else ""

    run_record = _build_run_record(
        attempt=new_state.attempts,
        started_at=started_at,
        finished_at=finished_at,
        plan=plan,
        summary=summary,
        cap_violation=cap_violation,
        process_exit_code=process.returncode if process.returncode is not None else -1,
        stderr_tail=stderr_tail,
    )

    final_state = _finalize_state(
        prior=new_state,
        plan=plan,
        run=run_record,
        summary=summary,
        cap_violation=cap_violation,
        settings_failure_classifier=settings_failure_classifier,
    )

    # Stop-and-ask override: if the agent wrote a sidecar request that has
    # no matching response, the agent has paused for an operator decision.
    # Mark the task awaiting_sidecar regardless of how the subprocess
    # exited — clean exit, error, or cap. The orchestrator's eligibility
    # check skips awaiting_sidecar tasks, so the slot frees for the next
    # pending task while this one waits for the operator.
    has_open_sidecar = any(tid == task.id for tid, _seq, _path in list_open_sidecars(queue_dir))
    if has_open_sidecar:
        final_state = final_state.model_copy(update={"status": "awaiting_sidecar"})

    if persist_state:
        write_state_atomic(final_state, state_path_for(queue_dir, task.id))

    # Post-dispatch hook (best-effort).
    post = hooks_mod.run_post_dispatch(
        settings_hooks,
        task,
        attempt=run_record.attempt,
        session_id=final_state.session_id,
        cwd=task.working_dir,
    )
    if post is not None and (post.timed_out or post.exit_code != 0):
        logger.warning(
            "post-dispatch hook for %s exited %d (timed_out=%s): %s",
            task.id,
            post.exit_code,
            post.timed_out,
            post.stderr.strip(),
        )

    # If we just attempted a RESUME and it errored quickly, the
    # supervisor's next tick will see resume_attempts incremented and
    # plan FRESH next time. Captured here for telemetry.
    _ = (settings_session, fall_through_to_fresh)  # referenced for static analysis

    return DispatchOutcome(
        run_record=run_record,
        new_state=final_state,
        summary=summary,
    )


def _count_trailing_failures(runs: list[RunRecord]) -> int:
    """Count consecutive failure RunRecords at the tail of ``runs``.

    A success interleaves the failure run and resets the count. The
    most recent run is the last element of ``runs``.

    Mirrors the criteria in `_finalize_state` for "completed": empty
    error AND a clean stop_reason. Anything else is a failure for
    circuit-breaker accounting purposes.
    """
    n = 0
    for record in reversed(runs):
        is_success = record.error is None and record.stop_reason in (
            "end_turn",
            "result",
        )
        if is_success:
            break
        n += 1
    return n


def _finalize_state(
    *,
    prior: TaskState,
    plan: SpawnPlan,
    run: RunRecord,
    summary: StreamSummary,
    cap_violation: caps_mod.CapViolation | None,
    settings_failure_classifier: FailureClassifierSettings | None = None,
) -> TaskState:
    """Apply a RunRecord to a TaskState, returning the post-attempt state.

    When ``settings_failure_classifier`` is supplied AND consecutive
    failures (including this one) reach the configured
    ``failure_circuit_breaker_threshold``, the status is set to
    ``failed_circuit_breaker`` instead of plain ``failed`` -- the
    orchestrator excludes that status from re-dispatch, breaking the
    auto-retry loop. Without this gate, a task that fails the same
    way every attempt (e.g. agent exits with ``stop_sequence`` and
    no real output) gets re-dispatched indefinitely because
    ``_DISPATCHABLE_STATUSES = {"pending", "failed"}``.
    """
    new_runs = [*prior.runs, run]

    if cap_violation is not None:
        new_status = "failed"
    elif run.error is None and run.stop_reason in ("end_turn", "result"):
        new_status = "completed"
    else:
        new_status = "failed"

    # Trip the circuit breaker on consecutive failures so we don't
    # auto-retry forever. The threshold is queue-configured under
    # `[failure_classifier]`; default is 3.
    if new_status == "failed" and settings_failure_classifier is not None:
        consecutive = _count_trailing_failures(new_runs)
        if retry_mod.circuit_breaker_tripped(
            consecutive_failures=consecutive,
            settings=settings_failure_classifier,
        ):
            new_status = "failed_circuit_breaker"

    new_session_id = summary.session_id or prior.session_id
    new_resume_attempts = (
        prior.resume_attempts + 1
        if plan.strategy is ResumeStrategy.RESUME
        else prior.resume_attempts
    )

    return prior.model_copy(
        update={
            "status": new_status,
            "session_id": new_session_id,
            "resume_attempts": new_resume_attempts,
            "last_finished_at": run.finished_at,
            "last_heartbeat_at": run.finished_at,
            "stop_reason": run.stop_reason,
            "error": run.error,
            "runs": new_runs,
        }
    )


def _record_pre_dispatch_failure(
    *,
    task: Task,
    state: TaskState,
    plan: SpawnPlan,
    queue_dir: Path,
    hook_result: hooks_mod.HookResult,
    clock: Clock,
    persist_state: bool,
    settings_failure_classifier: FailureClassifierSettings | None = None,
) -> DispatchOutcome:
    """When the pre-dispatch hook fails, write a RunRecord and TaskState
    showing the failure without ever spawning ``claude``.

    Routes through :func:`_finalize_state` so consecutive hook failures
    are counted toward the circuit-breaker threshold. Without that,
    a perma-deferring hook (e.g. ``DEFERRED: <paper> awaiting trim``)
    re-attempts forever, since the orchestrator treats ``failed`` as
    dispatchable — observed live with 71 consecutive
    ``pre_dispatch_hook_failed`` attempts on the same task, starving
    the rest of the queue at every tick.
    """
    started_at = clock.now()
    finished_at = clock.now()
    run = RunRecord(
        attempt=state.attempts + 1,
        started_at=started_at,
        finished_at=finished_at,
        stop_reason="pre_dispatch_hook_failed",
        error=(
            f"pre-dispatch hook exited {hook_result.exit_code}"
            + (" (timed out)" if hook_result.timed_out else "")
            + (f": {hook_result.stderr.strip()}" if hook_result.stderr.strip() else "")
        ),
        usage=TokenUsage(),
        cost_usd=0.0,
        duration_s=(finished_at - started_at).total_seconds(),
        resumed_from_session=plan.session_id if plan.strategy is ResumeStrategy.RESUME else None,
    )
    # Bump attempts on the "prior" state we hand to _finalize_state so
    # it ends up with the same attempts count as a normal run path
    # would produce. (The normal dispatch flow sets `attempts =
    # state.attempts + 1` BEFORE the run; _finalize_state preserves
    # `prior.attempts`.)
    prior_with_bumped_attempts = state.model_copy(
        update={
            "attempts": run.attempt,
            "last_started_at": started_at,
        }
    )
    new_state = _finalize_state(
        prior=prior_with_bumped_attempts,
        plan=plan,
        run=run,
        summary=StreamSummary(),
        cap_violation=None,
        settings_failure_classifier=settings_failure_classifier,
    )
    if persist_state:
        os.makedirs(state_path_for(queue_dir, task.id).parent, exist_ok=True)
        write_state_atomic(new_state, state_path_for(queue_dir, task.id))
    return DispatchOutcome(
        run_record=run,
        new_state=new_state,
        summary=StreamSummary(),
    )
