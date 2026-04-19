from __future__ import annotations

from datetime import UTC
from pathlib import Path
from typing import Any

import pytest

from claude_runner.cli import main
from claude_runner.config import load_settings
from claude_runner.state.store import StateStore


def test_version_flag(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0


def test_init_creates_project(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["init", str(tmp_path)])
    assert rc == 0
    assert (tmp_path / "claude_runner.toml").exists()
    assert (tmp_path / "todo").is_dir()
    # Example task must validate.
    settings = load_settings(tmp_path)
    assert settings.plan in {"pro", "max5", "max20", "team", "custom"}


def test_new_creates_task_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    main(["init", "."])
    rc = main(["new", "Example", "--dir", str(tmp_path / "todo"), "--effort", "low"])
    assert rc == 0
    created = list((tmp_path / "todo").glob("*-example.yaml"))
    assert created


def test_validate_reports_errors(tmp_path: Path) -> None:
    main(["init", str(tmp_path)])
    # Write a broken task file.
    (tmp_path / "todo" / "bad.yaml").write_text(
        "prompt: x\n", encoding="utf-8"
    )  # missing working_dir
    rc = main(["validate", str(tmp_path)])
    assert rc != 0


def test_validate_passes_on_valid_dir(tmp_path: Path) -> None:
    main(["init", str(tmp_path)])
    rc = main(["validate", str(tmp_path)])
    assert rc == 0


def test_run_end_to_end_with_fake_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive the full `run` command with a patched backend factory."""
    main(["init", str(tmp_path)])
    main(["new", "first", "--dir", str(tmp_path / "todo")])
    main(["new", "second", "--dir", str(tmp_path / "todo")])

    from claude_runner import cli as cli_mod
    from claude_runner.models import DispatchResult, StopReason, TaskStatus, TokenUsage

    class FakeBackend:
        name = "fake"

        def __init__(self, *, state_store: StateStore, emitter: Any) -> None:
            self._state = state_store

        async def run_task(self, spec):
            import asyncio
            from datetime import datetime

            state = self._state.load(spec.id)
            state.status = TaskStatus.COMPLETED
            state.stop_reason = StopReason.END_TURN
            state.session_id = f"sid-{spec.id}"
            state.last_finished_at = datetime.now(tz=UTC)
            self._state.save(state)
            await asyncio.sleep(0)
            return DispatchResult(
                task_id=spec.id,
                success=True,
                usage=TokenUsage(input_tokens=10, output_tokens=5),
                stop_reason=StopReason.END_TURN,
                session_id=f"sid-{spec.id}",
                duration_s=0.1,
            )

    monkeypatch.setattr(
        cli_mod,
        "_build_backend",
        lambda settings, *, state_store, emitter, sidecar_store=None: FakeBackend(
            state_store=state_store, emitter=emitter
        ),
    )
    rc = main(["run", str(tmp_path)])
    assert rc == 0
    store = StateStore(tmp_path / ".claude_runner")
    statuses = {s.task_id: s.status for s in store.iter_states()}
    assert all(v.value == "completed" for v in statuses.values())
    # Events file populated.
    events = store.events_path().read_text().splitlines()
    assert any('"run_started"' in line for line in events)
    assert any('"run_finished"' in line for line in events)


def test_status_command_runs(tmp_path: Path) -> None:
    main(["init", str(tmp_path)])
    rc = main(["status", str(tmp_path)])
    assert rc == 0


def test_resume_command(tmp_path: Path) -> None:
    main(["init", str(tmp_path)])
    # Pre-write state as failed.
    from claude_runner.models import TaskState, TaskStatus

    store = StateStore(tmp_path / ".claude_runner")
    store.save(TaskState(task_id="x", status=TaskStatus.FAILED, error="prev"))
    rc = main(["resume", "x", str(tmp_path)])
    assert rc == 0
    assert store.load("x").status is TaskStatus.PENDING
    assert store.load("x").error is None


def test_verbose_flag_sets_logging() -> None:
    # -vv should not raise.
    with pytest.raises(SystemExit):
        main(["-vv", "--version"])


def test_verbose_single_v_sets_info_level(tmp_path: Path) -> None:
    """-v (count=1) selects INFO."""
    rc = main(["-v", "init", str(tmp_path)])
    assert rc == 0


def test_verbose_double_v_sets_debug_level(tmp_path: Path) -> None:
    """-vv selects DEBUG (this is the level=logging.DEBUG branch)."""
    rc = main(["-vv", "init", str(tmp_path)])
    assert rc == 0


def test_unknown_command_exits_via_parser_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The branch in main() that catches an unknown args.command calls
    parser.error, which raises SystemExit. argparse normally rejects unknown
    commands at parse time, so to hit the branch we patch the parser to let
    anything through."""
    import argparse as _argparse

    from claude_runner import cli as cli_mod

    original = cli_mod._build_parser

    def parser_allowing_unknown() -> _argparse.ArgumentParser:
        p = original()

        # Replace parse_args with a no-validation version that returns a fake
        # namespace with command="bogus".
        def parse(_argv):
            return _argparse.Namespace(command="bogus", verbose=0)

        p.parse_args = parse  # type: ignore[method-assign]
        return p

    monkeypatch.setattr(cli_mod, "_build_parser", parser_allowing_unknown)
    with pytest.raises(SystemExit):
        cli_mod.main([])


def test_run_leaves_terminal_states_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-existing COMPLETED / FAILED / PENDING states should NOT be
    rewritten by the re-queue pass at the start of `run`."""
    main(["init", str(tmp_path)])
    store = StateStore(tmp_path / ".claude_runner")
    from claude_runner.models import TaskState, TaskStatus

    store.save(TaskState(task_id="done", status=TaskStatus.COMPLETED))
    store.save(TaskState(task_id="already-failed", status=TaskStatus.FAILED))
    store.save(TaskState(task_id="pending", status=TaskStatus.PENDING))

    from claude_runner import cli as cli_mod
    from claude_runner.runner.scheduler import SchedulerOutcome

    async def fake_run(_self):
        return SchedulerOutcome(completed=0, failed=0, breaker_tripped=False, breaker_reason=None)

    monkeypatch.setattr(cli_mod.Scheduler, "run", fake_run)
    main(["run", str(tmp_path)])

    assert store.load("done").status is TaskStatus.COMPLETED
    assert store.load("already-failed").status is TaskStatus.FAILED
    assert store.load("pending").status is TaskStatus.PENDING


def test_run_requeues_running_and_interrupted_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any task left RUNNING/INTERRUPTED/QUEUED from a prior session is
    normalized before the scheduler runs."""
    main(["init", str(tmp_path)])
    store = StateStore(tmp_path / ".claude_runner")
    from claude_runner.models import TaskState, TaskStatus

    # RUNNING with session id → should become INTERRUPTED
    store.save(TaskState(task_id="r1", status=TaskStatus.RUNNING, session_id="sid-r1"))
    # INTERRUPTED without session id → should become PENDING
    store.save(TaskState(task_id="r2", status=TaskStatus.INTERRUPTED))
    # QUEUED without session id → should become PENDING
    store.save(TaskState(task_id="r3", status=TaskStatus.QUEUED))

    from claude_runner import cli as cli_mod

    class NoopBackend:
        name = "noop"

        def __init__(self, *, state_store: StateStore, emitter: Any) -> None:
            self._state = state_store

        async def run_task(self, spec):
            import asyncio
            from datetime import datetime

            from claude_runner.models import DispatchResult, StopReason, TokenUsage

            state = self._state.load(spec.id)
            state.status = TaskStatus.COMPLETED
            state.last_finished_at = datetime.now(tz=UTC)
            self._state.save(state)
            await asyncio.sleep(0)
            return DispatchResult(
                task_id=spec.id,
                success=True,
                usage=TokenUsage(),
                stop_reason=StopReason.END_TURN,
                session_id=None,
                duration_s=0.01,
            )

    monkeypatch.setattr(
        cli_mod,
        "_build_backend",
        lambda settings, *, state_store, emitter, sidecar_store=None: NoopBackend(
            state_store=state_store, emitter=emitter
        ),
    )
    rc = main(["run", str(tmp_path)])
    assert rc == 0
    assert store.load("r1").status is TaskStatus.INTERRUPTED
    assert store.load("r2").status is TaskStatus.PENDING
    assert store.load("r3").status is TaskStatus.PENDING


def test_run_handles_keyboard_interrupt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ctrl-C during scheduler.run() should emit `interrupted` and exit 130."""
    main(["init", str(tmp_path)])
    from claude_runner import cli as cli_mod

    def raising(_coro):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_mod.asyncio, "run", raising)
    rc = main(["run", str(tmp_path)])
    assert rc == 130
    store = StateStore(tmp_path / ".claude_runner")
    events = store.events_path().read_text().splitlines()
    assert any('"interrupted"' in line for line in events)


def test_run_breaker_tripped_returns_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the scheduler outcome has breaker_tripped=True, CLI returns 2."""
    main(["init", str(tmp_path)])
    main(["new", "only", "--dir", str(tmp_path / "todo")])
    from claude_runner import cli as cli_mod
    from claude_runner.runner.scheduler import SchedulerOutcome

    async def fake_run(_self):
        return SchedulerOutcome(
            completed=0, failed=1, breaker_tripped=True, breaker_reason="synthetic"
        )

    monkeypatch.setattr(cli_mod.Scheduler, "run", fake_run)
    rc = main(["run", str(tmp_path)])
    assert rc == 2


def test_run_returns_1_when_some_tasks_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main(["init", str(tmp_path)])
    main(["new", "only", "--dir", str(tmp_path / "todo")])
    from claude_runner import cli as cli_mod
    from claude_runner.runner.scheduler import SchedulerOutcome

    async def fake_run(_self):
        return SchedulerOutcome(completed=0, failed=3, breaker_tripped=False, breaker_reason=None)

    monkeypatch.setattr(cli_mod.Scheduler, "run", fake_run)
    rc = main(["run", str(tmp_path)])
    assert rc == 1


def test_build_source_picks_ccusage_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """_build_source returns CCUsageSource when ccusage is installed."""
    from claude_runner import cli as cli_mod
    from claude_runner.budget.sources.ccusage import CCUsageSource
    from claude_runner.config import Settings

    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/ccusage" if name == "ccusage" else None
    )
    source = cli_mod._build_source(Settings(budget_source="ccusage"))
    assert isinstance(source, CCUsageSource)


def test_build_source_falls_back_to_context_when_ccusage_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from claude_runner import cli as cli_mod
    from claude_runner.budget.sources.context_cmd import ContextCmdSource
    from claude_runner.config import Settings

    monkeypatch.setattr("shutil.which", lambda _: None)
    source = cli_mod._build_source(Settings(budget_source="ccusage"))
    assert isinstance(source, ContextCmdSource)


def test_build_source_context_mode() -> None:
    from claude_runner import cli as cli_mod
    from claude_runner.budget.sources.context_cmd import ContextCmdSource
    from claude_runner.config import Settings

    source = cli_mod._build_source(Settings(budget_source="context"))
    assert isinstance(source, ContextCmdSource)


def test_build_source_api_headers_mode() -> None:
    from claude_runner import cli as cli_mod
    from claude_runner.budget.sources.api_headers import ApiHeadersSource
    from claude_runner.config import Settings

    source = cli_mod._build_source(Settings(budget_source="api_headers"))
    assert isinstance(source, ApiHeadersSource)


def test_build_source_static_returns_none() -> None:
    from claude_runner import cli as cli_mod
    from claude_runner.config import Settings

    assert cli_mod._build_source(Settings(budget_source="static")) is None


def test_build_backend_subprocess(tmp_path: Path) -> None:
    """backend='subprocess' produces a SubprocessBackend."""
    from claude_runner import cli as cli_mod
    from claude_runner.config import Settings
    from claude_runner.notify.emitter import EventEmitter
    from claude_runner.runner.subprocess_backend import SubprocessBackend

    store = StateStore(tmp_path / ".claude_runner")
    emitter = EventEmitter(
        events_path=store.events_path(), log_dir=store.root / "logs", stdout=False
    )
    backend = cli_mod._build_backend(
        Settings(backend="subprocess"), state_store=store, emitter=emitter
    )
    assert isinstance(backend, SubprocessBackend)


def test_build_backend_asyncio_default(tmp_path: Path) -> None:
    from claude_runner import cli as cli_mod
    from claude_runner.config import Settings
    from claude_runner.notify.emitter import EventEmitter
    from claude_runner.runner.asyncio_backend import AsyncioBackend

    store = StateStore(tmp_path / ".claude_runner")
    emitter = EventEmitter(
        events_path=store.events_path(), log_dir=store.root / "logs", stdout=False
    )
    backend = cli_mod._build_backend(
        Settings(backend="asyncio"), state_store=store, emitter=emitter
    )
    assert isinstance(backend, AsyncioBackend)


def test_resume_running_task_leaves_status_alone(tmp_path: Path) -> None:
    """The resume CLI only requeues terminal or interrupted states; RUNNING
    stays RUNNING (it'll be handled by the re-queue path on `run`)."""
    main(["init", str(tmp_path)])
    from claude_runner.models import TaskState, TaskStatus

    store = StateStore(tmp_path / ".claude_runner")
    store.save(TaskState(task_id="rr", status=TaskStatus.RUNNING, error="x"))
    rc = main(["resume", "rr", str(tmp_path)])
    assert rc == 0
    # Status unchanged for non-terminal, non-interrupted states.
    assert store.load("rr").status is TaskStatus.RUNNING
    # Error field is cleared unconditionally.
    assert store.load("rr").error is None
