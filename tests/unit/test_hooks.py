"""Tests for runner.hooks — pre/post-dispatch shell command runner."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from claude_task_runner.config.schema import HookSettings
from claude_task_runner.queue.schema import Task
from claude_task_runner.runner.hooks import (
    HookResult,
    run_post_dispatch,
    run_pre_dispatch,
)


def _hooks(
    *, pre: str = "", post: str = "", pre_timeout: float = 30, post_timeout: float = 30
) -> HookSettings:
    return HookSettings(
        pre_dispatch_command=pre,
        pre_dispatch_timeout_s=pre_timeout,
        post_dispatch_command=post,
        post_dispatch_timeout_s=post_timeout,
    )


def _task(*, working_dir: Path | None = None) -> Task:
    return Task(
        id="001-test",
        title="t",
        prompt="p",
        working_dir=working_dir,
    )


class TestPreDispatch:
    def test_unset_returns_none(self) -> None:
        result = run_pre_dispatch(_hooks(pre=""), _task(), attempt=1)
        assert result is None

    def test_success_zero_exit(self) -> None:
        result = run_pre_dispatch(_hooks(pre="true"), _task(), attempt=1)
        assert result is not None
        assert result.exit_code == 0
        assert result.timed_out is False

    def test_failure_nonzero_exit(self) -> None:
        result = run_pre_dispatch(_hooks(pre="false"), _task(), attempt=1)
        assert result is not None
        assert result.exit_code != 0

    def test_env_vars_exported(self, tmp_path: Path) -> None:
        # Use a python script to print env vars and verify them.
        script = tmp_path / "echo.py"
        script.write_text(
            "import os, sys\n"
            "sys.stdout.write(os.environ['TASK_ID'] + '|' + "
            "os.environ['TASK_MODEL'] + '|' + os.environ['ATTEMPT'])\n"
        )
        result = run_pre_dispatch(
            _hooks(pre=f"{sys.executable} {script}"),
            _task(),
            attempt=3,
            session_id="sess-abc",
        )
        assert result is not None
        assert result.exit_code == 0
        assert result.stdout == "001-test|claude-opus-5|3"

    def test_session_id_empty_when_none(self, tmp_path: Path) -> None:
        script = tmp_path / "echo_session.py"
        script.write_text("import os, sys\nsys.stdout.write('SID=' + os.environ['SESSION_ID'])\n")
        result = run_pre_dispatch(
            _hooks(pre=f"{sys.executable} {script}"),
            _task(),
            attempt=1,
            session_id=None,
        )
        assert result is not None
        assert result.stdout == "SID="

    def test_timeout(self) -> None:
        # `sleep 5` with 0.5s timeout
        result = run_pre_dispatch(_hooks(pre="sleep 5", pre_timeout=0.5), _task(), attempt=1)
        assert result is not None
        assert result.timed_out is True
        assert result.exit_code == -1
        assert "timed out" in result.stderr.lower()

    def test_shell_prefix_uses_shell(self, tmp_path: Path) -> None:
        result = run_pre_dispatch(
            _hooks(pre='shell:echo "hello world" && echo done'),
            _task(),
            attempt=1,
        )
        assert result is not None
        assert result.exit_code == 0
        assert "hello world" in result.stdout
        assert "done" in result.stdout

    def test_no_shell_prefix_treats_command_literally(self) -> None:
        # `echo "hello world"` without shell should still work via shlex
        result = run_pre_dispatch(
            _hooks(pre="echo hello"),
            _task(),
            attempt=1,
        )
        assert result is not None
        assert result.exit_code == 0
        assert "hello" in result.stdout


class TestPostDispatch:
    def test_unset_returns_none(self) -> None:
        assert run_post_dispatch(_hooks(post=""), _task(), attempt=1, session_id=None) is None

    def test_runs_command(self) -> None:
        result = run_post_dispatch(
            _hooks(post="true"),
            _task(),
            attempt=1,
            session_id="sess-xyz",
        )
        assert result is not None
        assert result.exit_code == 0


class TestHookResult:
    def test_immutable(self) -> None:
        r = HookResult(
            command="x",
            exit_code=0,
            stdout="o",
            stderr="e",
            duration_s=0.1,
            timed_out=False,
        )
        # Frozen dataclass raises FrozenInstanceError on attribute set.
        with pytest.raises(AttributeError):
            r.exit_code = 1  # type: ignore[misc]
