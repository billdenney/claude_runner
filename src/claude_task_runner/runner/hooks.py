"""Pre/post-dispatch shell hooks.

The runner exposes generic shell hooks (per ADR-0013) so each project
can run arbitrary pre/post commands without runner code changes — e.g.
the ``tracking/sync_worktrees.py`` step the existing mAb queue runs.

Behaviour:

* **Pre-dispatch**: a non-zero exit aborts that task's dispatch. Stderr
  is captured and surfaced to the operator.
* **Post-dispatch**: a non-zero exit logs a warning but does NOT mark
  the task failed — by then the actual claude work is done.

Environment variables exposed to the hook:

* ``$TASK_ID`` — the task's id
* ``$TASK_WORKING_DIR`` — :attr:`Task.working_dir`, or empty string
* ``$TASK_MODEL`` — :attr:`Task.model`
* ``$ATTEMPT`` — the current attempt number (string, ``"1"``, ``"2"``, …)
* ``$SESSION_ID`` — current session id, or empty string

Hooks run as subprocesses with a configurable timeout. They are NOT
shell-evaluated by default — the configured command string is split
with :func:`shlex.split` and passed directly to ``subprocess.run``. To
opt into shell evaluation, prefix the command with ``shell:``::

    [hooks]
    pre_dispatch_command = "shell:cd $TASK_WORKING_DIR && python sync.py"
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from claude_task_runner.config.schema import HookSettings
from claude_task_runner.queue.schema import Task


class HookError(RuntimeError):
    """Hook subprocess failed or timed out."""


@dataclass(frozen=True)
class HookResult:
    """Outcome of one hook invocation."""

    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool


def _build_env(task: Task, *, attempt: int, session_id: str | None) -> dict[str, str]:
    env = os.environ.copy()
    env["TASK_ID"] = task.id
    env["TASK_WORKING_DIR"] = str(task.working_dir) if task.working_dir else ""
    env["TASK_MODEL"] = task.model
    env["ATTEMPT"] = str(attempt)
    env["SESSION_ID"] = session_id or ""
    return env


def _run_command(
    command: str,
    *,
    timeout_s: float,
    env: dict[str, str],
    cwd: Path | None,
) -> HookResult:
    """Execute the command, return a :class:`HookResult`.

    A ``shell:`` prefix toggles ``shell=True``; otherwise the command is
    parsed with :func:`shlex.split` for safer argument quoting.
    """
    use_shell = command.startswith("shell:")
    if use_shell:
        actual = command[len("shell:") :]
        argv: str | list[str] = actual
    else:
        argv = shlex.split(command)
        if not argv:
            raise HookError(f"empty command after parsing: {command!r}")

    timed_out = False
    try:
        completed = subprocess.run(
            argv,
            shell=use_shell,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
            cwd=str(cwd) if cwd else None,
        )
        return HookResult(
            command=command,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_s=0.0,  # subprocess.run doesn't expose; caller can wrap if needed
            timed_out=False,
        )
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = (
            exc.stdout
            if isinstance(exc.stdout, str)
            else (exc.stdout or b"").decode("utf-8", errors="replace")
        )
        stderr = (
            exc.stderr
            if isinstance(exc.stderr, str)
            else (exc.stderr or b"").decode("utf-8", errors="replace")
        )
        return HookResult(
            command=command,
            exit_code=-1,
            stdout=stdout,
            stderr=stderr or f"hook timed out after {timeout_s}s",
            duration_s=float(timeout_s),
            timed_out=timed_out,
        )


def run_pre_dispatch(
    settings: HookSettings,
    task: Task,
    *,
    attempt: int,
    session_id: str | None = None,
    cwd: Path | None = None,
) -> HookResult | None:
    """Run the pre-dispatch hook, if configured.

    Returns ``None`` when no command is configured. Caller should
    abort dispatch when ``result.exit_code != 0`` or
    ``result.timed_out``.
    """
    if not settings.pre_dispatch_command.strip():
        return None
    env = _build_env(task, attempt=attempt, session_id=session_id)
    return _run_command(
        settings.pre_dispatch_command,
        timeout_s=settings.pre_dispatch_timeout_s,
        env=env,
        cwd=cwd,
    )


def run_post_dispatch(
    settings: HookSettings,
    task: Task,
    *,
    attempt: int,
    session_id: str | None,
    cwd: Path | None = None,
) -> HookResult | None:
    """Run the post-dispatch hook, if configured.

    Failure is logged but does NOT fail the task.
    """
    if not settings.post_dispatch_command.strip():
        return None
    env = _build_env(task, attempt=attempt, session_id=session_id)
    return _run_command(
        settings.post_dispatch_command,
        timeout_s=settings.post_dispatch_timeout_s,
        env=env,
        cwd=cwd,
    )
