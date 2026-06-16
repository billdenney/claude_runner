"""Dispatch a task as a ``claude`` subprocess and stream its output.

This is the top of the runner stack. It composes:

* :mod:`runner.session` to decide RESUME vs. FRESH spawn strategy.
* :mod:`runner.stream` to parse the NDJSON event stream.
* :mod:`runner.caps` to enforce per-task token/duration ceilings.
* :mod:`runner.heartbeat` to surface silent / hung subprocesses.
* :mod:`runner.hooks` to run pre/post-dispatch shell commands.
* :mod:`queue.store` to persist :class:`TaskState` updates atomically.

Subprocess lifecycle:

1. ``run_pre_dispatch`` — abort with an error RunRecord on non-zero exit.
2. Build argv via :func:`build_argv` (uses :class:`SpawnPlan`).
3. ``Popen`` with ``stdout=PIPE``, line-buffered text mode, and
   ``start_new_session=True`` so a cap-kill can signal the whole
   process group. The pid is then persisted to state for the
   silent-orphan reaper, and a background :class:`_DispatcherAliveMonitor`
   ticks ``dispatcher_alive_at`` independently of event arrival.
4. Each line is parsed by :func:`parse_lines`, which updates the
   :class:`StreamSummary` (cumulative usage, session id, final result)
   as a side effect; every event also refreshes the in-memory
   last-heartbeat time.
5. After every event, evaluate :func:`runner.caps.evaluate_caps` and
   :func:`runner.heartbeat.evaluate`; ``_terminate`` (SIGTERM→SIGKILL
   on the group) fires on cap breach or kill-level silence. The
   heartbeat is also written back to state, rate-limited to one write
   per ``heartbeat_persist_interval_s``.
6. On EOF, drain the remainder (stats-only re-parse), then build a
   :class:`RunRecord` from the final ResultEvent.
7. ``_finalize_state`` classifies completed/failed, applies the
   circuit-breaker and the ADR-0020 output-evidence gate, and persists.
8. ``run_post_dispatch`` (warning on failure, never task-failing).

The function is **synchronous and blocking** — one subprocess at a
time. The supervisor calls :func:`dispatch` in a thread (or
ProcessPoolExecutor) when ``target_concurrency > 1``.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO

from claude_task_runner.clock import Clock
from claude_task_runner.config.schema import (
    DispatchSettings,
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
    load_state,
    state_path_for,
    write_state_atomic,
)
from claude_task_runner.runner import add_dirs as add_dirs_mod
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
    StreamSummary,
    parse_lines,
)

logger = logging.getLogger(__name__)

# After this many heartbeat-persist failures within a single dispatch
# loop, escalate the per-failure log from WARNING to ERROR. One stray
# failure is a transient hiccup (a momentarily-busy disk); a run of them
# points at a real, sustained problem (disk full, read-only filesystem)
# the operator needs to see at ERROR level.
_HEARTBEAT_PERSIST_FAIL_ESCALATE_AFTER = 5


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


# Bash patterns that an autonomous extraction agent must be able to
# run end-to-end without an interactive operator. ``--permission-mode
# acceptEdits`` only auto-grants Write/Edit; shell commands still
# require either a per-invocation interactive grant (impossible in
# ``--print`` mode) or an explicit allow pattern at session start.
#
# Without these, the Lowe 2009 omalizumab agent (observed 2026-05-21)
# completed Phases 1-5 — read source PDFs, drafted the model + vignette,
# wrote NEWS.md, wrote the operator-handoff report — then hit "Claude
# requested permissions to run this command" prompts on the Phase 6
# steps (``Rscript -e 'nlmixr2lib::buildModelDb()'``, ``git add ...``,
# ``git commit``, ``git push``) and exited via ``end_turn`` with the
# work uncommitted and unpushed. The operator then had to rebase,
# regen registry artifacts, commit, and push by hand — wasteful and
# defeats the autonomous-dispatch design.
#
# The list is conservative: enumerated explicit prefixes only, no
# wildcards alone (would gate the whole tool). Add patterns here if
# a queue regularly needs another command; this list is what every
# dispatched agent sees by default.
_DEFAULT_BASH_ALLOW_PATTERNS: tuple[str, ...] = (
    # Version control. Read-only (status, log, diff, branch) and
    # write (add, commit, push, fetch, rebase, restore) are all here.
    "Bash(git *)",
    # R script invocations: package building (``Rscript -e
    # 'devtools::load_all(); buildModelDb()'``), vignette render
    # (``Rscript -e 'rmarkdown::render(...)'``), convention check
    # (``Rscript -e 'nlmixr2lib::checkModelConventions(...)'``), and
    # the full check pipeline (``Rscript -e 'devtools::check()'``).
    "Bash(Rscript *)",
    "Bash(R *)",
    # Build orchestration (``make check`` etc.).
    "Bash(make *)",
)


def _bash_allow_settings_json() -> str:
    """Build the ``--settings`` JSON string used to widen permissions.

    Returns a JSON document (as a string) with a single
    ``permissions.allow`` list. Claude Code merges this with any
    project-local ``settings.local.json``, so per-repo allow lists
    remain in effect — these are the strictly additional patterns
    the runner injects on every dispatch.
    """
    import json as _json

    payload = {"permissions": {"allow": list(_DEFAULT_BASH_ALLOW_PATTERNS)}}
    return _json.dumps(payload, separators=(",", ":"))


def build_argv(
    task: Task,
    plan: SpawnPlan,
    *,
    claude_executable: str = "claude",
    add_dirs: list[Path] | None = None,
) -> list[str]:
    """Build the argv for ``subprocess.Popen``.

    Always emits ``--print --output-format=stream-json --verbose`` (the
    combination that produces the NDJSON we parse). The ``--verbose``
    flag is required by the claude CLI when ``--print`` is paired with
    stream-json output.

    ``add_dirs`` (when non-empty) widens the spawned agent's sandbox
    beyond its cwd; each path is forwarded as ``--add-dir <path>``.
    The caller resolves the list via :mod:`runner.add_dirs` so that
    the queue dir is always present and per-task entries are
    validated.

    A ``--settings`` JSON is always emitted to grant the
    ``Bash(git *)``, ``Bash(Rscript *)``, ``Bash(R *)``, and
    ``Bash(make *)`` permissions — without these the autonomous
    agent can read sources and write files but cannot run the Phase 4
    convention check, the Phase 5 vignette render, or the Phase 6
    git commit + push, so it exits via ``end_turn`` with the work
    on disk but uncommitted. See ``_DEFAULT_BASH_ALLOW_PATTERNS``
    for the full list and rationale.
    """
    argv: list[str] = [
        claude_executable,
        "--print",
        "--output-format=stream-json",
        "--verbose",
        # Autonomous dispatch can't honour interactive permission grants;
        # the default permission policy in ``--print`` mode blocks every
        # first-time Write/Edit/Bash-redirect with "Claude requested
        # permissions to write to <path>, but you haven't granted it yet".
        # ``acceptEdits`` auto-grants file edits (Write/Edit and bash
        # redirects within the allowed-dir scope) while still gating
        # destructive shell operations and out-of-scope paths. Without
        # this flag the agent reads sources, drafts the model in
        # conversation, and exits cleanly via ``end_turn_no_output``
        # because every Write attempt was blocked (observed 2026-05-21
        # with task ``130-lowe_2009_omalizumab``).
        "--permission-mode",
        "acceptEdits",
        # See ``_DEFAULT_BASH_ALLOW_PATTERNS`` for why this is always set.
        "--settings",
        _bash_allow_settings_json(),
    ]
    if task.model:
        argv.extend(["--model", task.model])
    if task.allowed_tools:
        argv.extend(["--allowedTools", ",".join(task.allowed_tools)])
    for extra_dir in add_dirs or []:
        argv.extend(["--add-dir", str(extra_dir)])
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


# Poll cadence for the file tailer's no-data wait. Small enough that a
# worker exit is noticed promptly, large enough not to busy-spin a core
# while a quiet agent thinks. Module-level so tests can monkeypatch it
# to drive the loop deterministically without real sleeps.
_TAIL_POLL_INTERVAL_S = 0.1


def tail_lines(
    path: Path,
    *,
    alive: Callable[[], bool],
    poll_interval_s: float = _TAIL_POLL_INTERVAL_S,
    sleep_fn: Callable[[float], None] = time.sleep,
    read_fn: Callable[[Path, int], tuple[str, int]] | None = None,
) -> Iterator[str]:
    r"""Tail ``path`` line-by-line until the producer is gone.

    Yields only **complete, newline-terminated** lines (the trailing
    ``\n`` is included, matching what iterating ``process.stdout`` would
    produce so :func:`_read_lines` and this tailer are interchangeable
    inputs to :func:`_dispatch_loop`). A partial trailing line — bytes
    written without a terminating newline yet — is buffered and only
    emitted once its newline arrives.

    The ``alive`` predicate is the producer's liveness signal: the owned
    path passes ``lambda: process.poll() is None``; the adopted path
    passes ``lambda: <pid still alive>``. While ``alive()`` is True and
    no new data is available, the tailer sleeps ``poll_interval_s`` and
    retries (no busy-spin). Once ``alive()`` returns False the tailer
    does **one** final read of whatever has been flushed since the last
    poll, emits any remaining complete lines, and stops — so it never
    hangs after the worker exits but also never drops output written in
    the worker's final moments.

    The file may not exist yet when the tailer starts (the worker's
    ``Popen`` and the first write race the supervisor thread); a missing
    file reads as empty and is polled for, subject to the same
    ``alive()`` exit. Any bytes left un-terminated when the producer
    dies are intentionally **not** emitted — a half-written final line
    is corrupt and parsing it would risk a spurious event.

    The read offset is tracked across polls so each byte is read exactly
    once (the file is re-opened per poll rather than holding a handle for
    the whole — possibly multi-hour — run, so a long adoption never pins
    a file descriptor). ``read_fn`` / ``sleep_fn`` are injection seams
    for unit tests; the defaults read the real file from a byte offset
    and use ``time.sleep``.
    """
    if read_fn is None:
        read_fn = _read_from_offset

    buffer = ""
    offset = 0

    while True:
        producer_alive = alive()
        try:
            chunk, offset = read_fn(path, offset)
        except OSError as exc:
            # A transient read error (e.g. the file was rotated out from
            # under us) is logged and retried; a persistent one keeps the
            # loop polling until alive() goes False, which then exits.
            logger.warning("tail_lines: read of %s failed (%s); retrying", path, exc)
            chunk = ""

        if chunk:
            buffer += chunk
            while True:
                idx = buffer.find("\n")
                if idx == -1:
                    break
                line = buffer[: idx + 1]
                buffer = buffer[idx + 1 :]
                yield line

        if not producer_alive:
            # Producer was already gone before this read; the read above
            # was the final drain. Stop without sleeping so we don't hang.
            return

        sleep_fn(poll_interval_s)


def _read_from_offset(path: Path, offset: int) -> tuple[str, int]:
    """Read ``path`` from byte ``offset`` to EOF; return ``(text, new_offset)``.

    A missing file (the worker hasn't created it yet) reads as empty with
    the offset unchanged. Errors mode ``replace`` so a momentarily torn
    multi-byte sequence at a flush boundary doesn't raise — the parser
    already tolerates garbage lines (see :func:`runner.stream.parse_line`).
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            handle.seek(offset)
            chunk = handle.read()
            return chunk, handle.tell()
    except FileNotFoundError:
        return "", offset


# Bytes of the stderr file retained for the RunRecord error tail —
# matches the pipe-path's historical ``stderr[-500:]`` slice.
_STDERR_TAIL_BYTES = 500


def _attempt_log_paths(queue_dir: Path, task_id: str, attempt: int) -> tuple[Path, Path]:
    """Return ``(stdout_log, stderr_log)`` for one file-backed attempt.

    Layout (ADR-0025): ``<queue>/.claude_task_runner/logs/<task_id>/
    attempt-<attempt>.stream.jsonl`` for stdout (the parsed NDJSON
    stream) and ``attempt-<attempt>.stderr`` beside it. The per-task
    directory is created if missing. ``attempt`` is the 1-based attempt
    number already stored on ``TaskState.attempts`` for this run.
    """
    logs_dir = queue_dir / ".claude_task_runner" / "logs" / task_id
    logs_dir.mkdir(parents=True, exist_ok=True)
    stdout_log = logs_dir / f"attempt-{attempt}.stream.jsonl"
    stderr_log = _stderr_path_for_stdout_log(stdout_log)
    return stdout_log, stderr_log


def _stderr_path_for_stdout_log(stdout_log: Path) -> Path:
    """Map ``attempt-N.stream.jsonl`` → its sibling ``attempt-N.stderr``.

    The adopt path only has the stdout ``log_path`` recorded in state;
    this reconstructs the paired stderr file by stripping the
    ``.stream.jsonl`` double-suffix and appending ``.stderr``. Robust to
    the double extension (``Path.with_suffix`` only handles one level).
    """
    name = stdout_log.name
    base = name[: -len(".stream.jsonl")] if name.endswith(".stream.jsonl") else stdout_log.stem
    return stdout_log.with_name(f"{base}.stderr")


def _reparse_stdout_file(path: Path) -> StreamSummary:
    """Re-parse the complete stdout log into a fresh :class:`StreamSummary`.

    Used by the file-backed finalize so the summary reflects every event
    the worker wrote, including any final flush the live tailer raced
    past. Parsing from a clean ``StreamSummary`` (rather than mutating
    the loop's) guarantees cumulative usage is counted exactly once.
    A missing / unreadable file yields an empty summary — the finalize
    then classifies it as a crash-without-result (failed), which is the
    correct outcome for a worker that produced no parseable output.
    """
    summary = StreamSummary()
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for _ in parse_lines(handle, summary=summary):
                pass
    except OSError as exc:
        logger.warning("could not re-read stdout log %s for finalize: %s", path, exc)
    return summary


def _read_stderr_tail(path: Path, *, max_bytes: int = _STDERR_TAIL_BYTES) -> str:
    """Return the last ``max_bytes`` of the stderr log as text.

    Mirrors the pipe path's ``stderr[-500:]`` slice. Reads only the tail
    (seeks from the end) so a pathologically large stderr file is never
    slurped whole. A missing / unreadable file yields an empty string.
    """
    try:
        size = path.stat().st_size
        with open(path, "rb") as handle:
            if size > max_bytes:
                handle.seek(size - max_bytes)
            data = handle.read()
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""


class _DispatcherAliveMonitor:
    """Background thread that ticks ``dispatcher_alive_at`` on a fixed cadence.

    The dispatch loop blocks on ``process.stdout`` reads — when the
    subprocess emits no stream-json events, the loop's own heartbeat
    persist callback never fires and ``last_heartbeat_at`` goes stale.
    A monitor thread sidesteps this by writing ``dispatcher_alive_at``
    every ``interval_s`` independently of event arrival, so the
    supervisor's per-tick reaper can tell "agent quiet but dispatcher
    alive" (HEALTHY) from "dispatcher and agent both silent" (suspect
    zombie, fall through to filesystem verification).

    The thread is a daemon so a supervisor crash doesn't leak it; on
    a normal dispatch return the ``stop()`` call joins with a short
    timeout. A persist failure inside the loop is logged and the
    monitor keeps going — observability is best-effort and must never
    take down the parent dispatch.
    """

    def __init__(
        self,
        *,
        persist_fn: Callable[[datetime], None],
        clock: Clock,
        interval_s: float,
        task_id: str,
    ) -> None:
        self._persist_fn = persist_fn
        self._clock = clock
        self._interval_s = interval_s
        self._task_id = task_id
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"dispatcher-alive-monitor:{task_id}",
            daemon=True,
        )

    def start(self) -> None:
        """Persist the initial ``dispatcher_alive_at`` write and start the loop.

        The initial write happens on the caller's thread so the YAML
        field is non-None before ``dispatch()`` returns control to the
        loop — otherwise a very-fast subprocess could finalize before
        the monitor thread's first wake-up, leaving the field unset for
        the entire run.
        """
        try:
            self._persist_fn(self._clock.now())
        except Exception as exc:
            logger.warning(
                "task %s: initial dispatcher_alive persist failed (%s); continuing",
                self._task_id,
                exc,
            )
        self._thread.start()

    def stop(self) -> None:
        """Signal the thread to exit and wait briefly for it to join."""
        self._stop_event.set()
        # join() in case the test/supervisor doesn't care to wait, but
        # cap the wait at one interval so a stuck persist doesn't block
        # dispatch finalization.
        self._thread.join(timeout=self._interval_s + 1.0)

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval_s):
            try:
                self._persist_fn(self._clock.now())
            except Exception as exc:
                logger.warning(
                    "task %s: dispatcher_alive persist failed (%s); continuing",
                    self._task_id,
                    exc,
                )


def _pid_alive(pid: int) -> bool:
    """True iff ``pid`` is still a live process.

    ``os.kill(pid, 0)`` is the standard liveness probe. ``ESRCH``
    (``ProcessLookupError``) means the pid is genuinely gone → dead.
    ``EPERM`` (``PermissionError``) means the pid exists but is owned by
    another user (a Linux-user dispatch under ``sudo``) → treat as alive;
    we just can't signal it. Any other ``OSError`` is conservatively
    treated as alive so the adopt monitor doesn't finalize a worker
    prematurely on a transient probe failure (ADR-0025).
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        # ESRCH ⇒ genuinely gone; anything else ⇒ conservatively alive.
        return exc.errno != errno.ESRCH
    return True


def _resolve_self_user() -> str:
    """Return the supervisor's own Linux username; empty string on lookup failure.

    Mirrored from :mod:`doctor.checks._current_username`. The
    dispatcher uses it to decide whether ``linux_user`` requires a
    sudo prefix: if the configured target equals the supervisor's
    own user, no sudo is needed and we spawn directly.
    """
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    except Exception as exc:
        logger.debug("self-user lookup failed (%s); treating as unknown", exc)
        return ""


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
    account: str | None = None,
    pid: int | None = None,
) -> RunRecord:
    """Assemble a RunRecord from the dispatch loop's accumulated state.

    ``pid`` is the OS pid of the subprocess this run spawned, recorded
    in the historical run record so the orchestrator's tick-level reap
    can re-check liveness post-finalize and refuse to free the slot
    when the subprocess survived its kill (subprocess leak detection).
    """
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
        pid=pid,
        account=account,
    )


def _dispatch_loop(
    *,
    lines: Iterable[str],
    terminate: Callable[[], None],
    settings_caps: TaskCapsSettings,
    clock: Clock,
    task: Task,
    started_at: datetime,
    heartbeat_persist_fn: Callable[[datetime], None] | None = None,
) -> tuple[StreamSummary, caps_mod.CapViolation | None]:
    """Run the consume-and-monitor loop until the stream ends or we kill it.

    Returns the StreamSummary and any cap violation observed.

    The loop is decoupled from how its events arrive: ``lines`` is any
    iterable of NDJSON lines and ``terminate`` is the side-effect that
    stops the underlying worker on a cap/silence KILL. The owned path
    (same-incarnation ``Popen``) passes ``_read_lines(process.stdout)``
    or :func:`tail_lines` over the stdout log plus
    ``lambda: _terminate(process)``; the adopted path (a worker this
    supervisor did not spawn) passes :func:`tail_lines` over the
    recorded log file plus a ``killpg``-by-pid terminate. The per-event
    heartbeat / cap / silence logic below is identical across both —
    only the source of lines and the kill mechanism differ (ADR-0025).

    ``heartbeat_persist_fn``, when supplied, is invoked at most once
    per ``settings_caps.heartbeat_persist_interval_s`` seconds with
    the latest heartbeat timestamp. The caller writes that timestamp
    into the task's state YAML so the supervisor's per-tick silent-
    orphan reaper sees fresh liveness for healthy long-running tasks.
    The rate limit keeps a chatty subprocess (multiple events per
    second) from thrashing the filesystem — one write per interval
    is enough since the interval is well below
    ``heartbeat_silence_alert_s``.
    """
    summary = StreamSummary()
    cap_violation: caps_mod.CapViolation | None = None
    last_heartbeat: datetime | None = None
    last_persist_at: datetime | None = None
    persist_interval_s = settings_caps.heartbeat_persist_interval_s
    heartbeat_persist_failures = 0

    for _event in parse_lines(_read_lines(lines), summary=summary):
        # Every typed event ticks the in-memory heartbeat; the
        # in-process kill check below uses the un-coalesced value so a
        # KILL fires as soon as the threshold is crossed, even when the
        # YAML persist is still inside its rate-limit window.
        last_heartbeat = clock.now()

        # Persist the heartbeat to the state YAML so the supervisor's
        # per-tick reaper sees the freshness. Rate-limited to one write
        # per ``heartbeat_persist_interval_s`` because a chatty
        # subprocess (many events per second) would otherwise produce
        # one atomic write per event — needless I/O when one per
        # interval is enough for the reaper's threshold comparison.
        should_persist = heartbeat_persist_fn is not None and (
            last_persist_at is None
            or (last_heartbeat - last_persist_at).total_seconds() >= persist_interval_s
        )
        if should_persist:
            assert heartbeat_persist_fn is not None  # narrowed by guard above
            try:
                heartbeat_persist_fn(last_heartbeat)
            except Exception as exc:
                # First few failures are WARNING (likely transient); a
                # sustained run escalates to ERROR so a real disk problem
                # isn't lost in a sea of warnings.
                heartbeat_persist_failures += 1
                log = (
                    logger.error
                    if heartbeat_persist_failures >= _HEARTBEAT_PERSIST_FAIL_ESCALATE_AFTER
                    else logger.warning
                )
                log(
                    "task %s: heartbeat persist failed (%s); continuing [failure %d this dispatch]",
                    task.id,
                    exc,
                    heartbeat_persist_failures,
                )
            last_persist_at = last_heartbeat

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
            terminate()
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
            terminate()
            cap_violation = caps_mod.CapViolation(
                which="duration",
                observed=status.silence_s,
                cap=float(settings_caps.heartbeat_silence_kill_s),
            )
            break

    return summary, cap_violation


_MAX_ADD_DIRS_LOG_CHARS = 300


def _truncate_paths_for_log(paths: list[Path]) -> str:
    """Format a list of paths for a single log line, truncating if long.

    The dispatch log is one-line-per-attempt; an operator skimming
    ``journalctl`` for "what scope did this task get" wants the list
    inline rather than chasing a separate trace event. A pathological
    YAML with two dozen entries would still dominate the line, so
    cap the rendered string at ~300 chars and indicate truncation.
    """
    if not paths:
        return "[]"
    rendered = "[" + ", ".join(str(p) for p in paths) + "]"
    if len(rendered) <= _MAX_ADD_DIRS_LOG_CHARS:
        return rendered
    truncated = rendered[: _MAX_ADD_DIRS_LOG_CHARS - 4]
    return f"{truncated}...]"


def _signal_group_by_pid(pid: int, sig: int) -> None:
    """Send ``sig`` to the process group led by ``pid``.

    Workers are spawned with ``start_new_session=True`` so the worker is
    a session/group leader; signalling the group (rather than just
    ``pid``) reaches the ``claude`` process *and* any children it forked
    (MCP servers, shelled-out ``git``/``Rscript``). Without this a
    cap-kill would SIGTERM only the parent and leave its children
    running as orphans.

    A vanished group (``ProcessLookupError``) is benign — the process
    already exited between our check and the signal. Any other
    ``OSError`` (e.g. EPERM) is logged rather than swallowed so a
    genuinely failed signal-send surfaces in the journal. This is the
    shared core of both the owned-path :func:`_signal_group` and the
    adopted-path terminate (ADR-0025).
    """
    try:
        os.killpg(os.getpgid(pid), sig)
    except ProcessLookupError:
        # Group already gone; nothing to signal.
        pass
    except OSError as exc:
        logger.error("terminate signal failed pid=%s sig=%s: %s", pid, sig, exc)


def _signal_group(process: subprocess.Popen[str], sig: int) -> None:
    """Send ``sig`` to ``process``'s whole group (see :func:`_signal_group_by_pid`)."""
    _signal_group_by_pid(process.pid, sig)


def _terminate(process: subprocess.Popen[str]) -> None:
    """SIGTERM the subprocess's process group, escalate to SIGKILL, verify.

    Signals the whole group (see :func:`_signal_group`) so children
    forked by ``claude`` are reaped too. After a 5s grace period the
    group is SIGKILLed; a ``kill()`` that itself raises ``OSError`` (the
    group raced away under us) is logged, not swallowed.

    Post-kill verify: after the SIGKILL escalation we run a second
    ``process.wait(timeout=2)`` and log ERROR if the subprocess still
    has not reaped. A SIGKILL'd process that won't die is in
    ``TASK_UNINTERRUPTIBLE`` (D-state) — typically blocked in a kernel
    syscall — and there is nothing more userland can do. Logging here
    lets the supervisor's tick-level reap (:func:`_reap_finished`) see
    the leak and refuse to free the slot until operator intervention.
    """
    _signal_group(process, signal.SIGTERM)
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        _signal_group(process, signal.SIGKILL)
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        logger.error(
            "subprocess leak: pid=%s did not exit within 2s of group SIGKILL; "
            "likely TASK_UNINTERRUPTIBLE (D-state). Supervisor will refuse to "
            "free the dispatch slot until the kernel releases the process.",
            process.pid,
        )


def _terminate_by_pid(
    pid: int,
    *,
    alive: Callable[[], bool],
    sleep_fn: Callable[[float], None],
) -> None:
    """SIGTERM the group led by ``pid``, escalate to SIGKILL, verify.

    The adopted-worker analogue of :func:`_terminate`. Because we did
    NOT spawn the worker we have no ``Popen`` to ``wait()`` on; instead
    we poll the supplied ``alive`` predicate for up to the grace period
    and SIGKILL the group if it's still alive. Used as the ``terminate``
    callback in :func:`_dispatch_loop` for the adopted path so a cap or
    silence KILL still reaps a worker this supervisor never forked
    (ADR-0025).

    Post-kill verify: after the SIGKILL we poll ``alive`` for up to 2s.
    A worker that's still alive at that point is in
    ``TASK_UNINTERRUPTIBLE`` (D-state); we log ERROR so the supervisor
    can pick up the leak from the journal.
    """
    _signal_group_by_pid(pid, signal.SIGTERM)
    deadline = 5.0
    waited = 0.0
    step = 0.1
    while waited < deadline:
        if not alive():
            return
        sleep_fn(step)
        waited += step
    if not alive():
        return
    _signal_group_by_pid(pid, signal.SIGKILL)
    verify_deadline = 2.0
    waited = 0.0
    while waited < verify_deadline:
        if not alive():
            return
        sleep_fn(step)
        waited += step
    if alive():
        logger.error(
            "subprocess leak: adopted worker pid=%s did not exit within 2s of "
            "group SIGKILL; likely TASK_UNINTERRUPTIBLE (D-state). Supervisor "
            "will refuse to free the dispatch slot until the kernel releases "
            "the process.",
            pid,
        )


def _snapshot_pre_dispatch_sha(working_dir: Path | None) -> str | None:
    """Return the current ``HEAD`` SHA inside ``working_dir``, or ``None``.

    Used by the output-evidence gate (ADR-0020) to compare commits
    before vs. after dispatch. Failures (cwd not a git repo, ``git``
    missing, subprocess error) are swallowed with a warning — the gate
    falls back to skipping the commit check rather than blocking
    dispatch.
    """
    if working_dir is None:
        return None
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(working_dir),
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("pre-dispatch SHA snapshot failed for %s: %s", working_dir, exc)
        return None
    if completed.returncode != 0:
        logger.warning(
            "pre-dispatch SHA snapshot non-zero exit for %s (rc=%d): %s",
            working_dir,
            completed.returncode,
            completed.stderr.strip(),
        )
        return None
    sha = completed.stdout.strip()
    return sha or None


def _new_commit_since(working_dir: Path, pre_sha: str) -> bool:
    """True iff ``HEAD`` has moved past ``pre_sha`` since dispatch start."""
    try:
        completed = subprocess.run(
            ["git", "rev-list", "--count", f"{pre_sha}..HEAD"],
            cwd=str(working_dir),
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("post-dispatch commit check failed for %s: %s", working_dir, exc)
        return False
    if completed.returncode != 0:
        return False
    try:
        return int(completed.stdout.strip() or "0") > 0
    except ValueError:
        return False


@dataclass(frozen=True)
class OutputEvidence:
    """Why a clean-exit run is (or isn't) judged to have produced output.

    Set by :func:`_verify_output_evidence` and consumed in
    :func:`_finalize_state` to either pass the ``completed`` gate or
    flip the status to ``failed`` with stop_reason
    ``end_turn_no_output``.
    """

    has_commit: bool
    has_sidecar: bool
    has_deliverable: bool

    @property
    def any(self) -> bool:
        return self.has_commit or self.has_sidecar or self.has_deliverable

    def missed_gates(self) -> str:
        misses: list[str] = []
        if not self.has_commit:
            misses.append("no new commit on branch")
        if not self.has_sidecar:
            misses.append("no open sidecar")
        if not self.has_deliverable:
            misses.append("no declared deliverable on disk")
        return "; ".join(misses)


def _verify_output_evidence(
    *,
    task: Task,
    pre_sha: str | None,
    has_open_sidecar: bool,
) -> OutputEvidence:
    """Check whether a clean-exit run left an observable artifact behind.

    See ADR-0020. Returns an ``OutputEvidence`` describing which gates
    matched. The caller decides the consequence: at least one True ⇒
    ``completed``; all False ⇒ ``failed`` with stop_reason
    ``end_turn_no_output``.

    When ``task.working_dir is None`` the dispatcher caller skips
    calling this function entirely (existing non-worktree-task
    behavior is preserved).
    """
    working_dir = task.working_dir
    assert working_dir is not None  # caller ensures

    has_commit = pre_sha is not None and _new_commit_since(working_dir, pre_sha)

    has_deliverable = False
    for rel in task.deliverable_paths:
        candidate = rel if rel.is_absolute() else working_dir / rel
        if candidate.exists():
            has_deliverable = True
            break

    return OutputEvidence(
        has_commit=has_commit,
        has_sidecar=has_open_sidecar,
        has_deliverable=has_deliverable,
    )


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
    settings_dispatch: DispatchSettings | None = None,
    claude_executable: str = "claude",
    claude_config_dir: str = "",
    linux_user: str | None = None,
    account: str | None = None,
    persist_state: bool = True,
    adopt_workers: bool = False,
) -> DispatchOutcome:
    """Run one attempt for ``task`` and return the resulting state delta.

    Side effects (when ``persist_state=True``, the default):

    * Writes pre-run state with ``status="running"``.
    * Writes post-run state with appended :class:`RunRecord`, updated
      ``status``, and refreshed ``session_id``.

    The supervisor wraps this call with the throttling / concurrency
    decisions from :mod:`claude_task_runner.throttle.decision`.

    ``adopt_workers`` (ADR-0025): when True, the worker's stdout/stderr
    are redirected to per-attempt files under
    ``<queue>/.claude_task_runner/logs/<task_id>/`` and the dispatch
    loop tails the stdout file instead of reading a supervisor-owned
    pipe. The stdout log path is persisted to ``TaskState.log_path`` so
    a fresh supervisor can re-tail and *adopt* a still-running worker
    after a restart. When False (the default and the kill-switch path),
    the legacy pipe-backed behaviour is preserved bit-for-bit: pipes,
    drain on stop, demote-on-restart. ``persist_state=False`` (in-memory
    force-dispatch) forces the pipe path regardless, since there is no
    state YAML to record ``log_path`` for an adopter to find.
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
            account=account,
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

    # Resolve --add-dir scope: queue dir is always added; per-task
    # additional_dirs are merged in; prompt auto-detect is opt-in.
    auto_detect = (
        settings_dispatch.auto_detect_paths_in_prompt if settings_dispatch is not None else False
    )
    resolved_add_dirs = add_dirs_mod.resolve_add_dirs(
        task,
        queue_dir,
        auto_detect=auto_detect,
    )
    argv = build_argv(
        task,
        plan,
        claude_executable=claude_executable,
        add_dirs=resolved_add_dirs,
    )
    logger.info(
        "[dispatch] task_id=%s model=%s effort=%s attempt=%d add_dirs=%s",
        task.id,
        task.model,
        task.effort,
        new_state.attempts,
        _truncate_paths_for_log(resolved_add_dirs),
    )

    # Build the subprocess env. When `claude_config_dir` is set (per-queue
    # config selects a non-default Claude account, e.g. ~/.claude_personal),
    # propagate it via CLAUDE_CONFIG_DIR so the dispatched `claude --print`
    # subprocess reads the same credentials the supervisor's /usage capture
    # uses. Without this, the supervisor sees personal-account utilization
    # while every dispatched task hits the default ~/.claude account --
    # which may be at a different / depleted quota.
    #
    # PR 14: when the account is on a long-lived OAuth token (file at
    # `<config_dir>/oauth-token`, produced by `claude setup-token`),
    # also export `CLAUDE_CODE_OAUTH_TOKEN` so the CLI bypasses
    # `.credentials.json` and uses the long-lived bearer instead. This
    # is the same env-var contract documented for GitHub Actions; the
    # CLI honours it as the auth source when present. The file lookup
    # is shared with `ApiUsageSource` (PR 14) so the supervisor's
    # usage capture and the dispatched subprocess always agree on which
    # token is in use for an account.
    spawn_env: dict[str, str] | None = None
    if claude_config_dir:
        config_path = Path(claude_config_dir).expanduser()
        if not config_path.exists():
            raise DispatchError(f"CLAUDE_CONFIG_DIR does not exist: {config_path}")
        spawn_env = {**os.environ, "CLAUDE_CONFIG_DIR": str(config_path)}
        # Local import — keeps `oauth_token_file` out of the dispatcher's
        # cold-start path for single-account / pre-PR-14 deployments.
        from claude_task_runner.usage.oauth_token_file import read_long_lived_token

        long_lived = read_long_lived_token(claude_config_dir)
        if long_lived is not None:
            spawn_env["CLAUDE_CODE_OAUTH_TOKEN"] = long_lived

    # Multi-Linux-user dispatch: when the resolved account has a
    # `linux_user` set and it differs from the supervisor's own user,
    # wrap argv with `sudo -n -u <linux_user> env CLAUDE_CONFIG_DIR=...`.
    # The doctor's check_account_sudo runs `sudo -n -u <linux_user>
    # /bin/true` as a precondition at startup, so we don't re-check
    # here; -n keeps a misconfigured run from hanging on a password
    # prompt. ENV inheritance across sudo's user transition needs the
    # explicit ``env`` wrapper because sudo defaults strip the
    # supervisor's environment from the target's session.
    if linux_user and _resolve_self_user() != linux_user:
        sudo_path = shutil.which("sudo")
        if sudo_path is None:
            raise DispatchError(f"linux_user={linux_user!r} requested but sudo not on PATH")
        env_pairs: list[str] = []
        if spawn_env is not None:
            env_pairs.append(f"CLAUDE_CONFIG_DIR={spawn_env['CLAUDE_CONFIG_DIR']}")
            # PR 14: propagate the long-lived OAuth token across the
            # sudo boundary too — sudo's default env_reset would strip
            # CLAUDE_CODE_OAUTH_TOKEN. Without this the per-account
            # token would only affect the supervisor's own dispatches,
            # not the multi-Linux-user spawn (PR 3) target shells.
            if "CLAUDE_CODE_OAUTH_TOKEN" in spawn_env:
                env_pairs.append(f"CLAUDE_CODE_OAUTH_TOKEN={spawn_env['CLAUDE_CODE_OAUTH_TOKEN']}")
        # Propagate explicit env via `env` so sudo's default
        # env_reset behaviour doesn't drop CLAUDE_CONFIG_DIR.
        prefix: list[str] = [sudo_path, "-n", "-u", linux_user]
        if env_pairs:
            prefix.extend(["env", *env_pairs])
        argv = [*prefix, *argv]
        # Once the subprocess transitions to <linux_user>, the
        # supervisor's own env stops mattering — the env=... arg to
        # Popen below would only apply to the sudo invocation itself.
        # Clear it so we don't fight the sudo prefix.
        spawn_env = None

    # Pre-trust the task's working_dir (and mark onboarding complete) in the
    # target .claude.json. Idempotent — only writes when a flag changes.
    # ``--print`` mode doesn't paint the TUI trust prompt, but the trust
    # state is still consulted; without this entry a fresh config_dir can
    # bounce the dispatch on first use.
    from claude_task_runner.claude_init import ensure_initialized as _ensure_claude_init

    _trust_dir = task.working_dir if task.working_dir else Path.cwd()
    _ensure_claude_init(claude_config_dir or None, _trust_dir)

    # Snapshot HEAD before spawning so the post-run output-evidence
    # gate (ADR-0020) can detect a new commit. Failure here is a
    # warning, not an error — the gate degrades gracefully to checking
    # only sidecar/deliverable evidence.
    pre_sha = _snapshot_pre_dispatch_sha(task.working_dir)

    # ADR-0025: file-backed output. When adoption is on (and we have a
    # state YAML to record the log path on), redirect the worker's
    # stdout/stderr to per-attempt files so the worker is independent of
    # the supervisor's pipe lifetime — a supervisor exit can't EPIPE it,
    # and a fresh supervisor can re-tail the log to adopt the still-
    # running worker. ``persist_state=False`` (in-memory force-dispatch)
    # keeps the pipe path because there's no state YAML for an adopter
    # to find ``log_path`` on.
    use_files = adopt_workers and persist_state
    stdout_log_path: Path | None = None
    stderr_log_path: Path | None = None
    stdout_fh: IO[bytes] | None = None
    stderr_fh: IO[bytes] | None = None
    # PIPE for the legacy path, a real file fd for the file-backed path.
    # Keep ``text=True`` in both cases: ``text`` only governs decoding of
    # Popen-created PIPE streams, so it's a no-op for the file fds (the
    # child writes raw bytes through them either way) and lets ``process``
    # stay a uniform ``Popen[str]``.
    stdout_sink: IO[bytes] | int = subprocess.PIPE
    stderr_sink: IO[bytes] | int = subprocess.PIPE
    if use_files:
        stdout_log_path, stderr_log_path = _attempt_log_paths(
            queue_dir, task.id, new_state.attempts
        )
        # Open in binary write mode: the worker writes here for its whole
        # life and we tail it separately. We hold the write handles only
        # long enough to hand the fds to Popen, then close our copies (in
        # the finally below) — the child keeps its own dup'd fds. A
        # ``with`` block can't express this fd hand-off, hence the noqa.
        stdout_fh = open(stdout_log_path, "wb")  # noqa: SIM115 - fd handed to Popen, closed in finally
        stderr_fh = open(stderr_log_path, "wb")  # noqa: SIM115 - fd handed to Popen, closed in finally
        stdout_sink = stdout_fh
        stderr_sink = stderr_fh

    try:
        process = subprocess.Popen(  # caller-controlled
            argv,
            stdout=stdout_sink,
            stderr=stderr_sink,
            text=True,
            bufsize=1,
            cwd=str(task.working_dir) if task.working_dir else None,
            env=spawn_env,
            # New session ⇒ the subprocess is a process-group leader, so a
            # cap-kill (`_terminate`) can `os.killpg` the whole group and
            # reap children `claude` forked (MCP servers, shelled-out
            # git/Rscript). Without this the children survive as orphans.
            start_new_session=True,
        )
    finally:
        # The child has dup'd the fds; our copies are no longer needed.
        # Closing them lets the tailer (and the worker) own the file
        # without us pinning extra descriptors for the whole run.
        if stdout_fh is not None:
            stdout_fh.close()
        if stderr_fh is not None:
            stderr_fh.close()

    # Record the subprocess pid (and, for file-backed runs, the stdout
    # log path) on the TaskState so the supervisor's startup reaper /
    # adopter can find this worker after a restart. The original
    # "status=running" write above didn't carry the pid because Popen
    # hadn't fired yet; do a second atomic write now they're known.
    #
    # A failure here means this attempt is *untrackable*: the reaper
    # can't find the pid to clean up if the supervisor dies ungracefully
    # mid-run. We do NOT abort the dispatch — the subprocess has already
    # been spawned, so raising now would either leak the running child
    # (the very orphan pid-tracking exists to catch) or force us to kill
    # in-flight work over what is usually a transient disk hiccup.
    # Instead escalate to ERROR with an unmistakable UNTRACKED-PID marker
    # so an operator grepping the journal sees that this attempt won't be
    # reapable by pid.
    if persist_state:
        try:
            pid_update: dict[str, object] = {"pid": process.pid}
            if stdout_log_path is not None:
                pid_update["log_path"] = str(stdout_log_path)
            new_state = new_state.model_copy(update=pid_update)
            write_state_atomic(new_state, state_path_for(queue_dir, task.id))
        except Exception as exc:
            logger.error(
                "task %s: UNTRACKED-PID — failed to persist pid=%s for reap "
                "tracking (%s); dispatch continues but this attempt is not "
                "pid-reapable if the supervisor dies ungracefully",
                task.id,
                process.pid,
                exc,
            )

    # Persist heartbeats into the running-state YAML so the
    # supervisor's per-tick silent-orphan reaper sees fresh liveness.
    # The dispatcher's in-process kill check is event-driven and is
    # blind to a subprocess that wedges with zero events (the
    # 2026-06-12 ``frompeople-680-yu_2017`` zombie pattern); the
    # YAML-mediated reaper is the steady-state safety net.
    #
    # Two writers share the state YAML for the duration of the dispatch
    # loop:
    #
    # 1. ``heartbeat_persist_fn`` (in-loop, fires on stream-json events)
    #    updates ``last_heartbeat_at``.
    # 2. The dispatcher-alive monitor thread (below) updates
    #    ``dispatcher_alive_at`` on a fixed cadence regardless of events.
    #
    # The state lock serializes the read-modify-write so neither writer
    # clobbers the other's field. Both update ``new_state`` (the local
    # source of truth) and atomic-write the YAML.
    #
    # Only persist when ``persist_state`` is True — the dispatch
    # function's normal path. When the caller has opted into in-memory
    # mode (e.g. force_dispatch) there is no state YAML to update and
    # the reaper does not look for them.
    heartbeat_persist_fn: Callable[[datetime], None] | None
    alive_monitor: _DispatcherAliveMonitor | None
    if persist_state:
        state_lock = threading.Lock()
        state_path = state_path_for(queue_dir, task.id)

        def _persist_heartbeat(when: datetime) -> None:
            nonlocal new_state
            with state_lock:
                new_state = new_state.model_copy(update={"last_heartbeat_at": when})
                write_state_atomic(new_state, state_path)

        def _persist_alive(when: datetime) -> None:
            nonlocal new_state
            with state_lock:
                new_state = new_state.model_copy(update={"dispatcher_alive_at": when})
                write_state_atomic(new_state, state_path)

        heartbeat_persist_fn = _persist_heartbeat
        alive_monitor = _DispatcherAliveMonitor(
            persist_fn=_persist_alive,
            clock=clock,
            interval_s=settings_caps.dispatcher_alive_write_interval_s,
            task_id=task.id,
        )
        alive_monitor.start()
    else:
        heartbeat_persist_fn = None
        alive_monitor = None

    # Build the line source + terminate callback for the loop. Both
    # owned paths kill via the live Popen group; only the source of
    # lines differs (a file tailer when file-backed, the stdout pipe
    # otherwise). The terminate is wrapped so the loop stays oblivious
    # to whether it's reading a pipe or a file (ADR-0025).
    lines: Iterable[str]
    if use_files:
        assert stdout_log_path is not None
        lines = tail_lines(stdout_log_path, alive=lambda: process.poll() is None)
    else:
        assert process.stdout is not None
        lines = _read_lines(process.stdout)

    def _terminate_owned() -> None:
        _terminate(process)

    try:
        summary, cap_violation = _dispatch_loop(
            lines=lines,
            terminate=_terminate_owned,
            settings_caps=settings_caps,
            clock=clock,
            task=task,
            started_at=started_at,
            heartbeat_persist_fn=heartbeat_persist_fn,
        )
    finally:
        if alive_monitor is not None:
            alive_monitor.stop()

    # Drain remaining output and stderr. If the process won't finish
    # within the grace period, SIGKILL its whole group (so any forked
    # children die too) and drain again. `_signal_group` logs rather
    # than swallowing an OSError, so a kill that races the process
    # exiting is still surfaced.
    if use_files:
        # File-backed: there are no pipes to ``communicate`` over. Wait
        # for the worker (it writes its own log files), SIGKILL the
        # group on timeout. The tailer has already drained the stdout
        # file up to the worker's exit; any final bytes are re-read from
        # the file below. The stderr tail comes from the stderr file.
        assert stdout_log_path is not None
        assert stderr_log_path is not None
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _signal_group(process, signal.SIGKILL)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                logger.error(
                    "task %s: worker pid=%s did not exit after SIGKILL; finalizing anyway",
                    task.id,
                    process.pid,
                )
        # Re-read the complete stdout log into a FRESH summary so the
        # result reflects every event the worker wrote — the live tailer
        # stops the instant the worker exits and can miss the very last
        # flush. Reparsing the whole file into a new ``StreamSummary``
        # (rather than appending to the loop's) keeps cumulative usage
        # counted exactly once.
        summary = _reparse_stdout_file(stdout_log_path)
        stderr_tail = _read_stderr_tail(stderr_log_path)
    else:
        try:
            stdout_remainder, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            _signal_group(process, signal.SIGKILL)
            stdout_remainder, stderr = process.communicate()

        # Re-parse any remainder lines. parse_lines updates `summary`
        # (cumulative usage, session id, final result) as a side effect;
        # we iterate purely to drive that aggregation. Per-event handling
        # (heartbeat persist, cap/silence checks) is intentionally NOT
        # repeated here — the subprocess has already exited, so there is
        # nothing left to kill and the heartbeat is about to be
        # overwritten by `_finalize_state`. Remainder parsing is
        # stats-only.
        if stdout_remainder:
            for _ in parse_lines(stdout_remainder.splitlines(), summary=summary):
                pass
        stderr_tail = (stderr or "")[-500:] if stderr else ""

    finished_at = clock.now()

    run_record = _build_run_record(
        attempt=new_state.attempts,
        started_at=started_at,
        finished_at=finished_at,
        plan=plan,
        summary=summary,
        cap_violation=cap_violation,
        process_exit_code=process.returncode if process.returncode is not None else -1,
        stderr_tail=stderr_tail,
        account=account,
        pid=process.pid,
    )

    # Detect open sidecar once and thread it through finalization. The
    # output-evidence gate (ADR-0020) consults it when deciding whether
    # a clean exit really produced anything; the awaiting_sidecar
    # override below uses the same answer.
    has_open_sidecar = any(tid == task.id for tid, _seq, _path in list_open_sidecars(queue_dir))

    final_state, run_record = _finalize_state(
        prior=new_state,
        plan=plan,
        task=task,
        run=run_record,
        summary=summary,
        cap_violation=cap_violation,
        settings_failure_classifier=settings_failure_classifier,
        pre_sha=pre_sha,
        has_open_sidecar=has_open_sidecar,
    )

    # Stop-and-ask override: if the agent wrote a sidecar request that has
    # no matching response, the agent has paused for an operator decision.
    # Mark the task awaiting_sidecar regardless of how the subprocess
    # exited — clean exit, error, or cap. The orchestrator's eligibility
    # check skips awaiting_sidecar tasks, so the slot frees for the next
    # pending task while this one waits for the operator.
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


def adopt_worker(
    *,
    task: Task,
    state: TaskState,
    queue_dir: Path,
    clock: Clock,
    settings_caps: TaskCapsSettings,
    settings_failure_classifier: FailureClassifierSettings | None = None,
    account: str | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> DispatchOutcome:
    """Monitor a worker this supervisor did NOT spawn, to completion.

    ADR-0025 startup adoption. Given a ``state`` that is ``"running"``
    with a live ``pid`` and a present ``log_path``, this re-attaches to
    the orphaned ``claude --print`` worker without a ``Popen``:

    * liveness is ``os.kill(pid, 0)`` (``_pid_alive`` — ESRCH ⇒ dead,
      EPERM ⇒ alive);
    * the recorded stdout log is tailed via :func:`tail_lines` and fed
      through the SAME :func:`_dispatch_loop` as an owned worker, so the
      per-event cap / heartbeat / silence enforcement is identical;
    * a cap/silence KILL terminates the worker by pid
      (:func:`_terminate_by_pid` → ``killpg``);
    * the heartbeat + ``dispatcher_alive_at`` are refreshed on the state
      YAML while tailing, so the per-tick reaper sees HEALTHY and never
      demotes the adopted task out from under us.

    On completion (the pid is gone) the outcome is **inferred** from the
    terminal stream-json ``result`` event captured in the
    :class:`StreamSummary` — there is no ``returncode`` for a process we
    did not fork. If the pid vanished with no terminal result event the
    run is finalized as failed (the worker crashed mid-run). The
    finalize re-reads the on-disk status immediately before writing and
    stands down if a concurrent reaper already moved the task off
    ``"running"`` (the :func:`_demote_if_still_running` race guard),
    mirroring the reaper's own TOCTOU mitigation so neither clobbers the
    other.
    """
    started_at = state.last_started_at if state.last_started_at is not None else clock.now()
    log_path = Path(state.log_path) if state.log_path is not None else None
    pid = state.pid

    if pid is None or log_path is None:
        # Defensive: the caller (startup adoption) only adopts tasks with
        # both fields present. If we somehow get here, finalize as a
        # crash so the task isn't left stuck "running".
        logger.warning(
            "adopt_worker: task %s missing pid/log_path (pid=%s log_path=%s); "
            "finalizing as crashed",
            task.id,
            pid,
            log_path,
        )
        return _finalize_adopted(
            task=task,
            prior=state,
            summary=StreamSummary(),
            cap_violation=None,
            started_at=started_at,
            finished_at=clock.now(),
            stderr_tail="",
            account=account,
            queue_dir=queue_dir,
            settings_failure_classifier=settings_failure_classifier,
        )

    logger.info(
        "[adopt] task_id=%s pid=%s log=%s attempt=%d",
        task.id,
        pid,
        log_path,
        state.attempts,
    )

    # Shared running-state writer (heartbeat + dispatcher-alive) — same
    # lock discipline as dispatch()'s monitor so the two fields don't
    # clobber each other. ``new_state`` is the local source of truth.
    new_state = state
    state_lock = threading.Lock()
    state_path = state_path_for(queue_dir, task.id)

    def _persist_heartbeat(when: datetime) -> None:
        nonlocal new_state
        with state_lock:
            new_state = new_state.model_copy(update={"last_heartbeat_at": when})
            write_state_atomic(new_state, state_path)

    def _persist_alive(when: datetime) -> None:
        nonlocal new_state
        with state_lock:
            new_state = new_state.model_copy(update={"dispatcher_alive_at": when})
            write_state_atomic(new_state, state_path)

    alive_monitor = _DispatcherAliveMonitor(
        persist_fn=_persist_alive,
        clock=clock,
        interval_s=settings_caps.dispatcher_alive_write_interval_s,
        task_id=task.id,
    )
    alive_monitor.start()

    def _alive() -> bool:
        return _pid_alive(pid)

    def _terminate_adopted() -> None:
        _terminate_by_pid(pid, alive=_alive, sleep_fn=sleep_fn)

    try:
        summary, cap_violation = _dispatch_loop(
            lines=tail_lines(log_path, alive=_alive, sleep_fn=sleep_fn),
            terminate=_terminate_adopted,
            settings_caps=settings_caps,
            clock=clock,
            task=task,
            started_at=started_at,
            heartbeat_persist_fn=_persist_heartbeat,
        )
    finally:
        alive_monitor.stop()

    # The tailer exits once the pid is gone; re-read the full log so the
    # summary reflects the worker's final flush (the live tailer may stop
    # one read short of the terminal result event).
    summary = _reparse_stdout_file(log_path)
    stderr_tail = _read_stderr_tail(_stderr_path_for_stdout_log(log_path))
    finished_at = clock.now()

    return _finalize_adopted(
        task=task,
        prior=new_state,
        summary=summary,
        cap_violation=cap_violation,
        started_at=started_at,
        finished_at=finished_at,
        stderr_tail=stderr_tail,
        account=account,
        queue_dir=queue_dir,
        settings_failure_classifier=settings_failure_classifier,
    )


def _finalize_adopted(
    *,
    task: Task,
    prior: TaskState,
    summary: StreamSummary,
    cap_violation: caps_mod.CapViolation | None,
    started_at: datetime,
    finished_at: datetime,
    stderr_tail: str,
    account: str | None,
    queue_dir: Path,
    settings_failure_classifier: FailureClassifierSettings | None,
) -> DispatchOutcome:
    """Build the RunRecord + persist terminal state for an adopted worker.

    Exit code is inferred, not measured: a terminal ``result`` event in
    ``summary`` drives the classification (its ``stop_reason`` / error),
    exactly as the owned path would once ``_build_run_record`` consults
    ``summary.final_result``. With NO terminal result event we pass a
    non-zero ``process_exit_code`` so ``_build_run_record`` records a
    ``no_result`` failure — the correct outcome for a worker that
    vanished mid-run (crash, OOM-kill, or a kill we issued).

    The terminal write uses the same recheck guard as the silent-orphan
    reaper: if the on-disk status is no longer ``"running"`` a concurrent
    reaper finalized first, so we stand down rather than clobber its
    record (ADR-0025 concurrency note).
    """
    # No terminal result ⇒ infer a crash via a non-zero synthetic exit
    # code so _build_run_record records ``no_result``. A present result
    # event makes the exit code irrelevant (it branches on final_result).
    inferred_exit = 0 if summary.final_result is not None else -1

    # A "running" task always has attempts >= 1 (the dispatch that spawned
    # the worker bumped it). max(1, ...) is a defensive floor so a
    # hand-seeded / legacy state with attempts==0 can't trip RunRecord's
    # ``attempt >= 1`` constraint.
    run_record = _build_run_record(
        attempt=max(1, prior.attempts),
        started_at=started_at,
        finished_at=finished_at,
        plan=_ADOPT_PLAN,
        summary=summary,
        cap_violation=cap_violation,
        process_exit_code=inferred_exit,
        stderr_tail=stderr_tail,
        account=account,
        # Adopted workers were spawned by a prior supervisor incarnation;
        # the pid we know about is whatever the prior state YAML recorded.
        # Surface it on the run record so the orchestrator's subprocess-
        # leak check can probe it post-finalize, exactly like an owned-path
        # finalization.
        pid=prior.pid,
    )

    has_open_sidecar = any(tid == task.id for tid, _seq, _path in list_open_sidecars(queue_dir))

    final_state, run_record = _finalize_state(
        prior=prior,
        plan=_ADOPT_PLAN,
        task=task,
        run=run_record,
        summary=summary,
        cap_violation=cap_violation,
        settings_failure_classifier=settings_failure_classifier,
        pre_sha=None,
        has_open_sidecar=has_open_sidecar,
    )

    if has_open_sidecar:
        final_state = final_state.model_copy(update={"status": "awaiting_sidecar"})

    # ``pid`` and ``log_path`` are already cleared by ``_finalize_state``
    # (the attempt has terminated; nothing left to adopt or reap).

    # Recheck guard (ADR-0025): a per-tick reaper can demote this task
    # between our verdict and this write. Re-read; only persist if the
    # on-disk status is still "running" (i.e. nothing else finalized it).
    state_path = state_path_for(queue_dir, task.id)
    try:
        current = load_state(state_path)
    except Exception as exc:
        logger.warning(
            "adopt_worker: recheck-load of %s failed (%s); persisting finalize anyway",
            state_path,
            exc,
        )
        write_state_atomic(final_state, state_path)
    else:
        if current.status == "running":
            write_state_atomic(final_state, state_path)
        else:
            logger.info(
                "adopt_worker: task %s status changed to %s before adopt finalize; "
                "standing down so the concurrent writer's record wins",
                task.id,
                current.status,
            )
            final_state = current

    return DispatchOutcome(
        run_record=run_record,
        new_state=final_state,
        summary=summary,
    )


# A FRESH plan with no session id: adoption must not bump
# ``resume_attempts`` or record a ``resumed_from_session`` (we don't
# know whether the original spawn was a resume, and the session id is
# captured from the log's init event regardless). Shared singleton so
# the adopt path doesn't import SpawnPlan construction at every call.
_ADOPT_PLAN = SpawnPlan(
    strategy=ResumeStrategy.FRESH,
    session_id=None,
    prompt="",
    extra_args=[],
)


_SUCCESS_STOP_REASONS: frozenset[str] = frozenset(
    {
        # Model finished its turn naturally with no caller-imposed cap.
        "end_turn",
        # claude-code wrapper synthesises this for the top-level result
        # event when no specific stop_reason was provided by the API.
        "result",
        # Model emitted a configured stop sequence — this is a clean,
        # API-level "I'm done" signal, semantically equivalent to
        # end_turn from the runner's perspective (the dispatched agent
        # has finished its work and produced its output). Treating
        # stop_sequence as a failure caused the chen_2016 / garonzik_2016
        # / li_2015 false-positive failed-classifications observed live
        # on 2026-05-22/23 — each task had pushed a real commit, written
        # its report, and exited cleanly, but got re-dispatched because
        # the status was "failed".
        "stop_sequence",
    }
)
"""Stop reasons that indicate the dispatched agent finished its work
intentionally. Used by :func:`_finalize_state` and
:func:`_count_trailing_failures` so success classification and
circuit-breaker accounting stay in sync.

Failure-class stop_reasons (not in this set) include:
``max_tokens`` (cap exceeded mid-output), ``tool_use`` (turn handed
back to caller without resolution — should be picked up next tick
via session resume, not classified completed yet), and the runner-
synthesised ``killed_by_cap`` / ``process_exit_nonzero`` /
``no_result`` / ``pre_dispatch_hook_failed`` / ``end_turn_no_output``
(ADR-0020 evidence gate)."""


def _count_trailing_failures(runs: list[RunRecord]) -> int:
    """Count consecutive failure RunRecords at the tail of ``runs``.

    A success interleaves the failure run and resets the count. The
    most recent run is the last element of ``runs``.

    Mirrors the criteria in `_finalize_state` for "completed" via the
    shared :data:`_SUCCESS_STOP_REASONS` set: empty error AND a clean
    stop_reason. Anything else is a failure for circuit-breaker
    accounting purposes.
    """
    n = 0
    for record in reversed(runs):
        is_success = record.error is None and record.stop_reason in _SUCCESS_STOP_REASONS
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
    task: Task | None = None,
    pre_sha: str | None = None,
    has_open_sidecar: bool = False,
) -> tuple[TaskState, RunRecord]:
    """Apply a RunRecord to a TaskState, returning the post-attempt state
    and the (possibly amended) RunRecord.

    When ``settings_failure_classifier`` is supplied AND consecutive
    failures (including this one) reach the configured
    ``failure_circuit_breaker_threshold``, the status is set to
    ``failed_circuit_breaker`` instead of plain ``failed`` -- the
    orchestrator excludes that status from re-dispatch, breaking the
    auto-retry loop. Without this gate, a task that fails the same
    way every attempt (e.g. agent exits with ``stop_sequence`` and
    no real output) gets re-dispatched indefinitely because
    ``_DISPATCHABLE_STATUSES = {"pending", "failed"}``.

    A second gate (ADR-0020) flips a would-be ``completed`` status to
    ``failed`` with stop_reason ``end_turn_no_output`` when the run
    produced no observable artifact (no new commit on the worktree
    branch, no open sidecar, no declared deliverable on disk). The
    gate is opt-in: it only runs when ``task`` is supplied AND
    ``task.working_dir is not None``. The original-attempt RunRecord
    is amended so its ``stop_reason`` and ``error`` reflect the gate
    miss; the returned RunRecord is what callers should persist.
    """
    if cap_violation is not None:
        new_status = "failed"
    elif run.error is None and run.stop_reason in _SUCCESS_STOP_REASONS:
        new_status = "completed"
    else:
        new_status = "failed"

    # ADR-0020: gate "completed" on at least one observable output
    # artifact. Skip when the task has no working_dir (research/analysis
    # tasks intentionally run without a worktree; existing behavior is
    # preserved in that case).
    if new_status == "completed" and task is not None and task.working_dir is not None:
        evidence = _verify_output_evidence(
            task=task,
            pre_sha=pre_sha,
            has_open_sidecar=has_open_sidecar,
        )
        if not evidence.any:
            new_status = "failed"
            run = run.model_copy(
                update={
                    "stop_reason": "end_turn_no_output",
                    "error": (f"no observable output produced ({evidence.missed_gates()})"),
                }
            )

    new_runs = [*prior.runs, run]

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
    # Persist which account hosts the current session so the dispatcher
    # can honour session affinity on the next attempt (ADR-0024). Two
    # cases:
    #   * FRESH that produced a new session id → host account = the
    #     account this run executed on (run.account).
    #   * RESUME that succeeded → session id is unchanged from prior;
    #     the host account also stays unchanged (a successful resume
    #     proves the prior account still hosts it).
    # When this run produced no session id and the prior had none
    # (cold start that failed before SystemInitEvent), session_account
    # stays None — there's no session to be affined to.
    if new_session_id is None:
        new_session_account: str | None = None
    elif summary.session_id and summary.session_id != prior.session_id:
        new_session_account = run.account
    else:
        new_session_account = prior.session_account or run.account

    new_state = prior.model_copy(
        update={
            "status": new_status,
            "session_id": new_session_id,
            "session_account": new_session_account,
            "resume_attempts": new_resume_attempts,
            "last_finished_at": run.finished_at,
            "last_heartbeat_at": run.finished_at,
            "stop_reason": run.stop_reason,
            "error": run.error,
            "runs": new_runs,
            # Subprocess has exited (or been killed); clear the pid so
            # the supervisor's silent-orphan reaper doesn't try to
            # signal a now-recycled OS pid.
            "pid": None,
            # Clear the file-backed stream log pointer (ADR-0025): the
            # attempt has finalized, so a fresh supervisor must not try to
            # adopt this (now-terminal) task by re-tailing a stale log.
            # The next dispatch records a new log_path. No-op for the
            # pipe path, where log_path was never set.
            "log_path": None,
        }
    )
    return new_state, run


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
    account: str | None = None,
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
        account=account,
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
    new_state, run = _finalize_state(
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
