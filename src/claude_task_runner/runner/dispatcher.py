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
    except Exception:
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
        account=account,
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


def _terminate(process: subprocess.Popen[str]) -> None:
    """Try SIGTERM, escalate to SIGKILL after grace period."""
    try:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
    except OSError:
        pass


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
        account=account,
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

    new_state = prior.model_copy(
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
