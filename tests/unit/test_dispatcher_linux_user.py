"""Tests for the multi-Linux-user sudo spawn path in the dispatcher.

When a resolved account carries ``linux_user``, the dispatcher must
wrap the ``claude`` argv with ``sudo -n -u <linux_user> env ...``
so the spawned subprocess runs under that uid and still reads the
correct ``CLAUDE_CONFIG_DIR``. ``-n`` ensures sudo never hangs on a
password prompt; the operator wires passwordless sudo as a precondition
(verified by the doctor's check_account_sudo at startup).

Drives ``dispatch()`` with monkey-patched ``subprocess.Popen`` so the
test asserts on the argv that *would* be spawned without launching
real subprocesses.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import pytest

from claude_task_runner.config.schema import (
    DispatchSettings,
    FailureClassifierSettings,
    HookSettings,
    SessionSettings,
    TaskCapsSettings,
)
from claude_task_runner.queue.schema import Task, TaskState
from claude_task_runner.runner.dispatcher import (
    DispatchError,
    ResumeStrategy,
    SpawnPlan,
    dispatch,
)


class _FrozenClock:
    """Minimal Clock substitute for tests; advances on each .now() call."""

    def __init__(self) -> None:
        self._n = datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._n


class _FakePopen:
    """subprocess.Popen drop-in that captures argv and immediately exits 0.

    The dispatcher's stream-json loop iterates ``process.stdout`` so we
    expose an empty iterable. ``communicate`` returns ("", "") and
    ``returncode`` is 0 so the dispatch path lands the no-result branch
    in _build_run_record.
    """

    captured_argv: ClassVar[list[list[str]]] = []
    captured_env: ClassVar[list[dict[str, str] | None]] = []

    def __init__(self, argv: list[str], *args: object, **kwargs: object) -> None:
        type(self).captured_argv.append(list(argv))
        type(self).captured_env.append(kwargs.get("env"))
        self.returncode = 0
        # An empty iterable; the dispatcher iterates stdout via parse_lines.
        self.stdout = iter([])
        self.stderr = None
        self._argv = argv

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        return ("", "")

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass

    def poll(self) -> int | None:
        return 0


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    (qd / "todo").mkdir(parents=True)
    (qd / ".claude_task_runner" / "state").mkdir(parents=True)
    return qd


@pytest.fixture
def task() -> Task:
    return Task.model_validate({"id": "t1", "title": "Test", "prompt": "do thing"})


@pytest.fixture
def task_state() -> TaskState:
    return TaskState.model_validate({"task_id": "t1", "status": "pending"})


@pytest.fixture
def settings_kwargs() -> dict[str, object]:
    return {
        "settings_caps": TaskCapsSettings(
            max_tokens_per_task=0,
            max_duration_s_per_task=0,
            heartbeat_silence_alert_s=300,
            heartbeat_silence_kill_s=0,
        ),
        "settings_session": SessionSettings(max_resume_attempts=3, resume_fail_fast_s=5),
        "settings_hooks": HookSettings(
            pre_dispatch_command="",
            pre_dispatch_timeout_s=120,
            post_dispatch_command="",
            post_dispatch_timeout_s=60,
        ),
        "settings_failure_classifier": FailureClassifierSettings(
            environmental_patterns=[],
            operator_patterns=[],
            task_patterns=[],
            failure_circuit_breaker_threshold=3,
        ),
        "settings_dispatch": DispatchSettings(auto_detect_paths_in_prompt=False),
    }


def _plan() -> SpawnPlan:
    return SpawnPlan(strategy=ResumeStrategy.FRESH, session_id=None, prompt="x", extra_args=[])


def test_no_linux_user_spawns_directly(
    queue_dir: Path,
    task: Task,
    task_state: TaskState,
    settings_kwargs: dict[str, object],
) -> None:
    """linux_user=None → no sudo prefix; argv starts with the claude executable."""
    _FakePopen.captured_argv.clear()
    with (
        patch("subprocess.Popen", _FakePopen),
        patch("claude_task_runner.runner.dispatcher.shutil.which", return_value="/usr/bin/claude"),
        patch("claude_task_runner.claude_init.ensure_initialized"),
    ):
        dispatch(
            task=task,
            state=task_state,
            plan=_plan(),
            queue_dir=queue_dir,
            clock=_FrozenClock(),
            claude_executable="claude",
            claude_config_dir="",
            linux_user=None,
            account=None,
            persist_state=False,
            **settings_kwargs,
        )
    assert len(_FakePopen.captured_argv) == 1
    argv = _FakePopen.captured_argv[0]
    assert argv[0] == "claude"
    assert "sudo" not in argv[0]


def test_linux_user_same_as_self_skips_sudo(
    queue_dir: Path,
    task: Task,
    task_state: TaskState,
    settings_kwargs: dict[str, object],
) -> None:
    """linux_user equal to supervisor's user is a no-op (no sudo prefix)."""
    _FakePopen.captured_argv.clear()
    with (
        patch("subprocess.Popen", _FakePopen),
        patch("claude_task_runner.runner.dispatcher.shutil.which", return_value="/usr/bin/claude"),
        patch("claude_task_runner.runner.dispatcher._resolve_self_user", return_value="bill"),
        patch("claude_task_runner.claude_init.ensure_initialized"),
    ):
        dispatch(
            task=task,
            state=task_state,
            plan=_plan(),
            queue_dir=queue_dir,
            clock=_FrozenClock(),
            claude_executable="claude",
            claude_config_dir="",
            linux_user="bill",
            account="self",
            persist_state=False,
            **settings_kwargs,
        )
    argv = _FakePopen.captured_argv[0]
    assert argv[0] == "claude"


def test_linux_user_different_wraps_with_sudo(
    queue_dir: Path,
    task: Task,
    task_state: TaskState,
    settings_kwargs: dict[str, object],
    tmp_path: Path,
) -> None:
    """linux_user != supervisor user → argv starts with sudo -n -u <user>."""
    _FakePopen.captured_argv.clear()
    # The dispatcher requires claude_config_dir to exist if non-empty.
    cfg_dir = tmp_path / "claude_other"
    cfg_dir.mkdir()
    real_which = shutil.which

    def _which(name: str) -> str | None:
        if name == "claude":
            return "/usr/bin/claude"
        if name == "sudo":
            return "/usr/bin/sudo"
        return real_which(name)

    with (
        patch("subprocess.Popen", _FakePopen),
        patch("claude_task_runner.runner.dispatcher.shutil.which", side_effect=_which),
        patch("claude_task_runner.runner.dispatcher._resolve_self_user", return_value="bill"),
        patch("claude_task_runner.claude_init.ensure_initialized"),
    ):
        dispatch(
            task=task,
            state=task_state,
            plan=_plan(),
            queue_dir=queue_dir,
            clock=_FrozenClock(),
            claude_executable="claude",
            claude_config_dir=str(cfg_dir),
            linux_user="bill-work",
            account="work",
            persist_state=False,
            **settings_kwargs,
        )
    argv = _FakePopen.captured_argv[0]
    # The sudo prefix wraps the original argv. CLAUDE_CONFIG_DIR is
    # injected via `env` so sudo's env_reset doesn't drop it.
    assert argv[0] == "/usr/bin/sudo"
    assert argv[1] == "-n"
    assert argv[2] == "-u"
    assert argv[3] == "bill-work"
    assert argv[4] == "env"
    assert f"CLAUDE_CONFIG_DIR={cfg_dir}" in argv[5]
    # claude executable lives after the env CLAUDE_CONFIG_DIR=... pair.
    assert "claude" in argv[6]


def test_linux_user_without_sudo_binary_raises(
    queue_dir: Path,
    task: Task,
    task_state: TaskState,
    settings_kwargs: dict[str, object],
) -> None:
    """sudo missing but linux_user requested → DispatchError before spawning."""

    def _which(name: str) -> str | None:
        return "/usr/bin/claude" if name == "claude" else None

    _FakePopen.captured_argv.clear()
    with (
        patch("claude_task_runner.runner.dispatcher.shutil.which", side_effect=_which),
        patch("claude_task_runner.runner.dispatcher._resolve_self_user", return_value="bill"),
        patch("claude_task_runner.claude_init.ensure_initialized"),
        pytest.raises(DispatchError, match="sudo"),
    ):
        dispatch(
            task=task,
            state=task_state,
            plan=_plan(),
            queue_dir=queue_dir,
            clock=_FrozenClock(),
            claude_executable="claude",
            claude_config_dir="",
            linux_user="bill-work",
            account="work",
            persist_state=False,
            **settings_kwargs,
        )
    assert _FakePopen.captured_argv == []


def test_run_record_carries_account(
    queue_dir: Path,
    task: Task,
    task_state: TaskState,
    settings_kwargs: dict[str, object],
) -> None:
    """Each RunRecord records which account it was dispatched through."""
    _FakePopen.captured_argv.clear()
    with (
        patch("subprocess.Popen", _FakePopen),
        patch("claude_task_runner.runner.dispatcher.shutil.which", return_value="/usr/bin/claude"),
        patch("claude_task_runner.claude_init.ensure_initialized"),
    ):
        outcome = dispatch(
            task=task,
            state=task_state,
            plan=_plan(),
            queue_dir=queue_dir,
            clock=_FrozenClock(),
            claude_executable="claude",
            claude_config_dir="",
            linux_user=None,
            account="personal",
            persist_state=False,
            **settings_kwargs,
        )
    assert outcome.run_record.account == "personal"


def test_run_record_account_none_by_default(
    queue_dir: Path,
    task: Task,
    task_state: TaskState,
    settings_kwargs: dict[str, object],
) -> None:
    """Single-account legacy callers leave account=None."""
    _FakePopen.captured_argv.clear()
    with (
        patch("subprocess.Popen", _FakePopen),
        patch("claude_task_runner.runner.dispatcher.shutil.which", return_value="/usr/bin/claude"),
        patch("claude_task_runner.claude_init.ensure_initialized"),
    ):
        outcome = dispatch(
            task=task,
            state=task_state,
            plan=_plan(),
            queue_dir=queue_dir,
            clock=_FrozenClock(),
            claude_executable="claude",
            claude_config_dir="",
            persist_state=False,
            **settings_kwargs,
        )
    assert outcome.run_record.account is None
