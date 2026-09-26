"""The post-dispatch hook is best-effort on every finalize path.

``runner.dispatcher._run_post_dispatch_hook`` runs the configured
``[hooks].post_dispatch_command`` after the owned finalize and after the
adopted one (a worker adopted or found exited after a restart, ADR-0025).
The run is already recorded by then, so a hook that fails, or cannot even
start, must log a warning and never raise: on the startup exited-worker
pass, an exception would be reported as a failed finalize of a task that
was in fact recorded.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from claude_task_runner.config.loader import load_settings
from claude_task_runner.queue.schema import Task
from claude_task_runner.runner.dispatcher import _run_post_dispatch_hook

_HOOKS = load_settings(None).hooks


def _task(working_dir: Path | None = None) -> Task:
    return Task(id="040-hook", title="t", prompt="p", working_dir=working_dir)


@pytest.mark.parametrize(
    ("command", "working_dir", "expected_log"),
    [
        pytest.param(
            "/nonexistent/post-dispatch-hook",
            None,
            "post-dispatch hook for 040-hook could not run",
            id="missing-executable",
        ),
        pytest.param(
            'echo "unterminated',
            None,
            "post-dispatch hook for 040-hook could not run",
            id="unparseable-command",
        ),
        pytest.param(
            "true",
            Path("/nonexistent/worktree"),
            "post-dispatch hook for 040-hook could not run",
            id="missing-working-dir",
        ),
        pytest.param(
            "false",
            None,
            "post-dispatch hook for 040-hook exited 1 (timed_out=False)",
            id="non-zero-exit",
        ),
    ],
)
def test_a_failing_hook_logs_and_never_raises(
    caplog: pytest.LogCaptureFixture,
    command: str,
    working_dir: Path | None,
    expected_log: str,
) -> None:
    settings = _HOOKS.model_copy(update={"post_dispatch_command": command})

    with caplog.at_level(logging.WARNING, logger="claude_task_runner.runner.dispatcher"):
        _run_post_dispatch_hook(settings, _task(working_dir), attempt=1, session_id="s")

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, caplog.text
    assert warnings[0].startswith(expected_log), warnings[0]


def test_a_hook_runs_with_the_attempt_env(tmp_path: Path) -> None:
    marker = tmp_path / "hook-ran"
    settings = _HOOKS.model_copy(
        update={
            "post_dispatch_command": (
                f'shell:printf "%s %s %s" "$TASK_ID" "$ATTEMPT" "$SESSION_ID" > {marker}'
            )
        }
    )

    _run_post_dispatch_hook(settings, _task(), attempt=3, session_id="sess-9")

    assert marker.read_text() == "040-hook 3 sess-9"


def test_no_hook_configured_is_a_no_op(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="claude_task_runner.runner.dispatcher"):
        _run_post_dispatch_hook(_HOOKS, _task(), attempt=1, session_id=None)

    assert caplog.records == []
