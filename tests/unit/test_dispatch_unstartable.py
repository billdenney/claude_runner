"""A worker that cannot be started parks its task; it never stays ``running``.

:func:`runner.dispatcher.dispatch` checks, before it writes the ``running``
state, that the ``claude`` binary is on PATH, that the account's
``CLAUDE_CONFIG_DIR`` can be expanded and exists, that ``sudo`` is there for
a ``linux_user``, and that the task's ``working_dir`` can be expanded. When
one fails, or opening the attempt's log files or ``Popen`` raises after that
write, the task is parked ``deferred`` from the state it had before the
dispatch: no attempt is counted and nothing feeds the circuit breaker, so
a fix lets it run after ``deferral_recheck_cooldown_s`` with no operator
step. A ``running`` state with no worker would instead turn
``possibly_hung`` once the silent reaper's alert passed, and stay there.
"""

from __future__ import annotations

import functools
import logging
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import pytest

from claude_task_runner.clock import FakeClock
from claude_task_runner.config.loader import load_defaults, load_settings
from claude_task_runner.config.schema import (
    DispatchSettings,
    FailureClassifierSettings,
    HookSettings,
    TaskCapsSettings,
)
from claude_task_runner.queue.schema import Task, TaskState
from claude_task_runner.queue.store import load_state, state_path_for, write_state_atomic
from claude_task_runner.runner import dispatcher as dispatcher_mod
from claude_task_runner.runner import orchestrator as orchestrator_mod
from claude_task_runner.runner.dispatcher import (
    UNSTARTABLE_STOP_REASON,
    ResumeStrategy,
    SpawnPlan,
    dispatch,
)
from claude_task_runner.runner.spawn_gate import SpawnGate

DISPATCHER_LOGGER = "claude_task_runner.runner.dispatcher"
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
COOLDOWN_S = 60.0
GONE = "~no-such-user-ctr-test"
"""A ``~user`` that names no user."""


class _FakePopen:
    """A worker that starts, prints nothing and exits 0."""

    started: ClassVar[list[dict[str, object]]] = []

    def __init__(self, argv: list[str], *args: object, **kwargs: object) -> None:
        type(self).started.append({"argv": list(argv), **kwargs})
        self.returncode = 0
        self.stdout = iter([])
        self.stderr = None
        self.pid = 4242

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
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated ``$HOME``, so no test reaches the real ``~/.claude.json``."""
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    return path


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    (qd / "todo").mkdir(parents=True)
    (qd / ".claude_task_runner" / "state").mkdir(parents=True)
    return qd


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """An account's ``CLAUDE_CONFIG_DIR``, past onboarding."""
    path = tmp_path / "acct"
    path.mkdir()
    (path / ".claude.json").write_text("{}", encoding="utf-8")
    return path


@pytest.fixture
def written(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The status of every state the dispatcher writes, in order."""
    statuses: list[str] = []
    real = dispatcher_mod.write_state_atomic

    def record(state: TaskState, path: Path) -> None:
        statuses.append(state.status)
        real(state, path)

    monkeypatch.setattr(dispatcher_mod, "write_state_atomic", record)
    return statuses


def _task(working_dir: str | Path | None = None) -> Task:
    fields: dict[str, object] = {"id": "t1", "title": "T", "prompt": "do the thing"}
    if working_dir is not None:
        fields["working_dir"] = str(working_dir)
    return Task.model_validate(fields)


PRIOR = TaskState(task_id="t1", status="pending", attempts=2)
"""The state before the dispatch: two earlier attempts."""


def _dispatch(
    queue_dir: Path,
    task: Task,
    *,
    claude_config_dir: str = "",
    linux_user: str | None = None,
    adopt_workers: bool = False,
    spawn_gate: SpawnGate | None = None,
) -> dispatcher_mod.DispatchOutcome:
    write_state_atomic(PRIOR, state_path_for(queue_dir, task.id))
    return dispatch(
        task=task,
        state=PRIOR,
        plan=SpawnPlan(strategy=ResumeStrategy.FRESH, session_id=None, prompt="x", extra_args=[]),
        queue_dir=queue_dir,
        clock=FakeClock(NOW),
        settings_caps=TaskCapsSettings(
            max_tokens_per_task=0,
            max_duration_s_per_task=0,
            heartbeat_silence_alert_s=300,
            heartbeat_silence_kill_s=0,
        ),
        settings_hooks=HookSettings(
            pre_dispatch_command="",
            pre_dispatch_timeout_s=120,
            post_dispatch_command="",
            post_dispatch_timeout_s=60,
        ),
        settings_failure_classifier=FailureClassifierSettings(
            failure_circuit_breaker_threshold=3, deferral_recheck_cooldown_s=COOLDOWN_S
        ),
        settings_dispatch=DispatchSettings(auto_detect_paths_in_prompt=False),
        claude_executable="claude",
        claude_config_dir=claude_config_dir,
        linux_user=linux_user,
        account="work",
        persist_state=True,
        adopt_workers=adopt_workers,
        spawn_gate=spawn_gate,
    )


def _parked(error: str) -> TaskState:
    """The state a task whose worker could not be started is left in."""
    return PRIOR.model_copy(
        update={
            "status": "deferred",
            "deferral_count": 1,
            "deferred_reason": f"cannot start the worker: {error}",
            "next_eligible_at": NOW + timedelta(seconds=COOLDOWN_S),
            "last_started_at": NOW,
            "last_finished_at": NOW,
            "stop_reason": UNSTARTABLE_STOP_REASON,
        }
    )


def _which(*present: str) -> object:
    return patch(
        "claude_task_runner.runner.dispatcher.shutil.which",
        side_effect=lambda name: f"/usr/bin/{name}" if name in present else None,
    )


@pytest.fixture
def no_worker() -> Iterator[None]:
    """Fail the test if a ``claude`` worker is started; other processes
    (the ``git`` HEAD snapshot) run as usual."""
    real = subprocess.Popen

    def guard(argv: list[str], *args: object, **kwargs: object) -> object:
        if argv and argv[0] == "claude":
            raise AssertionError("a worker was started")
        return real(argv, *args, **kwargs)

    with patch("subprocess.Popen", guard):
        yield


def _assert_parked(
    queue_dir: Path,
    outcome: dispatcher_mod.DispatchOutcome,
    error: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    expected = _parked(error)
    assert outcome.new_state == expected
    assert load_state(state_path_for(queue_dir, "t1")) == expected
    assert outcome.run_record.stop_reason == UNSTARTABLE_STOP_REASON
    assert [
        r.getMessage()
        for r in caplog.records
        if r.name == DISPATCHER_LOGGER and r.levelno >= logging.ERROR
    ] == [f"task t1: cannot start the worker: {error}; deferred, next try in 60 s"]


def test_the_stop_reason_is_the_one_the_runbook_names() -> None:
    assert UNSTARTABLE_STOP_REASON == "worker_start_deferred"


class TestChecksBeforeTheRunningWrite:
    """Each check fails before anything is written but the deferral."""

    @pytest.mark.usefixtures("home", "no_worker")
    def test_no_claude_binary(
        self, queue_dir: Path, written: list[str], caplog: pytest.LogCaptureFixture
    ) -> None:
        with _which(), caplog.at_level(logging.INFO, logger=DISPATCHER_LOGGER):
            outcome = _dispatch(queue_dir, _task())
        _assert_parked(queue_dir, outcome, "claude binary not found: claude", caplog)
        assert written == ["deferred"]

    @pytest.mark.usefixtures("home", "no_worker")
    def test_a_config_dir_that_cannot_be_expanded(
        self, queue_dir: Path, written: list[str], caplog: pytest.LogCaptureFixture
    ) -> None:
        with _which("claude"), caplog.at_level(logging.INFO, logger=DISPATCHER_LOGGER):
            outcome = _dispatch(queue_dir, _task(), claude_config_dir=f"{GONE}/.claude")
        _assert_parked(
            queue_dir,
            outcome,
            f"config_dir '{GONE}/.claude' cannot be expanded: Could not determine home directory.",
            caplog,
        )
        assert written == ["deferred"]

    @pytest.mark.usefixtures("home", "no_worker")
    def test_a_config_dir_that_does_not_exist(
        self,
        tmp_path: Path,
        queue_dir: Path,
        written: list[str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        absent = tmp_path / "unmounted"
        with _which("claude"), caplog.at_level(logging.INFO, logger=DISPATCHER_LOGGER):
            outcome = _dispatch(queue_dir, _task(), claude_config_dir=str(absent))
        _assert_parked(queue_dir, outcome, f"CLAUDE_CONFIG_DIR does not exist: {absent}", caplog)
        assert written == ["deferred"]

    @pytest.mark.usefixtures("home", "no_worker")
    def test_sudo_missing_for_a_linux_user(
        self,
        queue_dir: Path,
        config_dir: Path,
        written: list[str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with (
            _which("claude"),
            patch("claude_task_runner.runner.dispatcher._resolve_self_user", return_value="bill"),
            caplog.at_level(logging.INFO, logger=DISPATCHER_LOGGER),
        ):
            outcome = _dispatch(
                queue_dir, _task(), claude_config_dir=str(config_dir), linux_user="bill-work"
            )
        _assert_parked(
            queue_dir, outcome, "linux_user='bill-work' requested but sudo not on PATH", caplog
        )
        assert written == ["deferred"]

    @pytest.mark.usefixtures("home", "no_worker")
    def test_a_working_dir_that_cannot_be_expanded(
        self,
        queue_dir: Path,
        config_dir: Path,
        written: list[str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with _which("claude"), caplog.at_level(logging.INFO, logger=DISPATCHER_LOGGER):
            outcome = _dispatch(queue_dir, _task(f"{GONE}/wt"), claude_config_dir=str(config_dir))
        _assert_parked(
            queue_dir,
            outcome,
            f"working_dir '{GONE}/wt' cannot be expanded: Could not determine home directory.",
            caplog,
        )
        assert written == ["deferred"]


class TestStartingTheProcess:
    """A process that fails to start replaces the ``running`` write."""

    @pytest.mark.usefixtures("home")
    def test_a_working_dir_that_does_not_exist(
        self,
        tmp_path: Path,
        queue_dir: Path,
        config_dir: Path,
        written: list[str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The real ``Popen`` cannot change into it, so nothing is executed."""
        missing = tmp_path / "worktree-not-created"
        gate = SpawnGate()
        with _which("claude"), caplog.at_level(logging.INFO, logger=DISPATCHER_LOGGER):
            outcome = _dispatch(
                queue_dir, _task(missing), claude_config_dir=str(config_dir), spawn_gate=gate
            )
        _assert_parked(
            queue_dir,
            outcome,
            f"[Errno 2] No such file or directory: '{missing}'",
            caplog,
        )
        assert written == ["running", "deferred"]
        # The gate saw the thread leave, so a stopping supervisor waits for nothing.
        assert gate.close(0) == []

    @pytest.mark.usefixtures("home")
    def test_log_files_that_cannot_be_opened(
        self,
        tmp_path: Path,
        queue_dir: Path,
        config_dir: Path,
        written: list[str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        nowhere = tmp_path / "no-logs-dir"
        with (
            _which("claude"),
            patch(
                "claude_task_runner.runner.dispatcher._attempt_log_paths",
                return_value=(nowhere / "stdout.jsonl", nowhere / "stderr.log"),
            ),
            patch("subprocess.Popen", side_effect=AssertionError("a worker was started")),
            caplog.at_level(logging.INFO, logger=DISPATCHER_LOGGER),
        ):
            outcome = _dispatch(
                queue_dir, _task(), claude_config_dir=str(config_dir), adopt_workers=True
            )
        _assert_parked(
            queue_dir,
            outcome,
            f"[Errno 2] No such file or directory: '{nowhere / 'stdout.jsonl'}'",
            caplog,
        )
        assert written == ["running", "deferred"]

    @pytest.mark.usefixtures("home")
    def test_an_argument_popen_refuses(
        self,
        queue_dir: Path,
        config_dir: Path,
        written: list[str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with (
            _which("claude"),
            patch("subprocess.Popen", side_effect=ValueError("embedded null byte")),
            caplog.at_level(logging.INFO, logger=DISPATCHER_LOGGER),
        ):
            outcome = _dispatch(queue_dir, _task(), claude_config_dir=str(config_dir))
        _assert_parked(queue_dir, outcome, "ValueError: embedded null byte", caplog)
        assert written == ["running", "deferred"]


# ---------------------------------------------------------------------------
# Through the orchestrator: parked, held for the cooldown, then run once fixed
# ---------------------------------------------------------------------------


def test_a_fixed_config_dir_runs_the_task_after_the_cooldown(
    tmp_path: Path, queue_dir: Path, home: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The orchestrator's dispatch thread parks the task, the selector holds
    it until the cooldown passes, and once the mount is up the next dispatch
    starts the worker as attempt 3."""
    mount = tmp_path / "mount"
    task = _task()
    (queue_dir / "todo" / "t1.yaml").write_text(
        "id: t1\ntitle: T\nprompt: do the thing\n", encoding="utf-8"
    )
    write_state_atomic(PRIOR, state_path_for(queue_dir, "t1"))
    settings = load_settings(None)
    settings = settings.model_copy(
        update={
            "failure_classifier": settings.failure_classifier.model_copy(
                update={"deferral_recheck_cooldown_s": COOLDOWN_S}
            ),
            "hooks": settings.hooks.model_copy(
                update={"pre_dispatch_command": "", "post_dispatch_command": ""}
            ),
            "supervisor": settings.supervisor.model_copy(update={"adopt_workers": False}),
        }
    )
    run = functools.partial(
        orchestrator_mod._dispatch_one_safely,
        task,
        queue_dir,
        settings,
        FakeClock(NOW),
        "claude",
        str(mount),
        None,
        "work",
    )
    eligible = functools.partial(
        orchestrator_mod._eligible_candidates,
        queue_dir,
        [],
        set(),
        effort_levels=load_defaults()["effort_levels"],
    )
    _FakePopen.started.clear()
    with _which("claude"), patch("subprocess.Popen", _FakePopen):
        run()
        parked = load_state(state_path_for(queue_dir, "t1"))
        assert (parked.status, parked.attempts, parked.deferred_reason) == (
            "deferred",
            2,
            f"cannot start the worker: CLAUDE_CONFIG_DIR does not exist: {mount}",
        )
        assert [t.id for t in eligible(now=NOW + timedelta(seconds=COOLDOWN_S - 1))] == []
        assert [t.id for t in eligible(now=NOW + timedelta(seconds=COOLDOWN_S))] == ["t1"]

        mount.mkdir()
        run()
    after = load_state(state_path_for(queue_dir, "t1"))
    assert after.attempts == 3
    assert len(after.runs) == 1
    assert after.status != "deferred"
    assert [started["env"]["CLAUDE_CONFIG_DIR"] for started in _FakePopen.started] == [str(mount)]
