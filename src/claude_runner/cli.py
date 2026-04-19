"""Command-line entry points."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from claude_runner import __version__
from claude_runner.budget.circuit_breaker import CircuitBreaker
from claude_runner.budget.controller import TokenBudgetController
from claude_runner.budget.sources import BudgetSource
from claude_runner.budget.sources.api_headers import ApiHeadersSource
from claude_runner.budget.sources.ccusage import CCUsageSource
from claude_runner.budget.sources.context_cmd import ContextCmdSource
from claude_runner.config import Settings, load_settings
from claude_runner.defaults import Effort
from claude_runner.notify.emitter import EventEmitter
from claude_runner.runner.asyncio_backend import AsyncioBackend
from claude_runner.runner.backend import RunnerBackend
from claude_runner.runner.scheduler import Scheduler
from claude_runner.runner.subprocess_backend import SubprocessBackend
from claude_runner.scaffold import init_project, write_new_task
from claude_runner.state.store import StateStore
from claude_runner.todo.catalog import TodoCatalog, full_load

_log = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="claude-runner", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="Scaffold a todo project directory")
    p_init.add_argument("directory", nargs="?", default=".", type=Path)

    p_new = sub.add_parser("new", help="Create a new task YAML with sensible defaults")
    p_new.add_argument("title")
    p_new.add_argument("--dir", dest="todo_dir", type=Path, default=None)
    p_new.add_argument("--effort", choices=[e.value for e in Effort], default=None)
    p_new.add_argument("--model", default=None)
    p_new.add_argument("--working-dir", type=Path, default=None)
    p_new.add_argument("--depends-on", action="append", default=None)
    p_new.add_argument("--prompt", default=None)

    p_run = sub.add_parser("run", help="Drain the todo queue")
    p_run.add_argument("project_dir", nargs="?", default=".", type=Path)

    p_status = sub.add_parser("status", help="Show queue and usage summary")
    p_status.add_argument("project_dir", nargs="?", default=".", type=Path)

    p_validate = sub.add_parser("validate", help="Parse every task YAML and report errors")
    p_validate.add_argument("project_dir", nargs="?", default=".", type=Path)

    p_resume = sub.add_parser("resume", help="Mark a single task for re-run")
    p_resume.add_argument("task_id")
    p_resume.add_argument("project_dir", nargs="?", default=".", type=Path)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    level = logging.WARNING
    if args.verbose == 1:
        level = logging.INFO
    elif args.verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")

    if args.command == "init":
        return _cmd_init(args)
    if args.command == "new":
        return _cmd_new(args)
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "status":
        return _cmd_status(args)
    if args.command == "validate":
        return _cmd_validate(args)
    if args.command == "resume":
        return _cmd_resume(args)
    parser.error(f"unknown command {args.command}")
    raise AssertionError  # unreachable — parser.error calls SystemExit


# ----- command implementations -----------------------------------------


def _cmd_init(args: argparse.Namespace) -> int:
    project_dir: Path = args.directory.resolve()
    settings = Settings()
    result = init_project(project_dir, settings=settings)
    console = Console()
    console.print(f"[bold green]Initialized[/] claude_runner project at {project_dir}")
    for k, v in result.items():
        console.print(f"  {k}: {v}")
    return 0


def _cmd_new(args: argparse.Namespace) -> int:
    project_dir = Path.cwd()
    settings = load_settings(project_dir)
    todo_dir = (args.todo_dir or (project_dir / settings.todo_subdir)).resolve()
    effort = Effort(args.effort) if args.effort else None
    path = write_new_task(
        title=args.title,
        todo_dir=todo_dir,
        settings=settings,
        prompt=args.prompt,
        working_dir=args.working_dir,
        effort=effort,
        model=args.model,
        depends_on=args.depends_on,
    )
    Console().print(f"[bold green]Wrote[/] {path}")
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    project_dir: Path = args.project_dir.resolve()
    settings = load_settings(project_dir)
    todo_dir = project_dir / settings.todo_subdir
    result = full_load(todo_dir, settings=settings)
    console = Console()
    if result.errors:
        for err in result.errors:
            console.print(f"[red]error[/] {err.path}: {err.message}")
    console.print(f"{len(result.tasks)} task(s) parsed, {len(result.errors)} error(s)")
    return 1 if result.errors else 0


def _cmd_status(args: argparse.Namespace) -> int:
    project_dir: Path = args.project_dir.resolve()
    settings = load_settings(project_dir)
    state_store = StateStore(project_dir / settings.state_subdir)
    catalog = TodoCatalog(
        project_dir / settings.todo_subdir, state_store=state_store, settings=settings
    )
    entries = catalog.all_entries()
    console = Console()
    table = Table(title=f"claude_runner status — {project_dir}")
    table.add_column("ID")
    table.add_column("Status")
    table.add_column("Attempts", justify="right")
    table.add_column("Last run")
    table.add_column("Session")
    table.add_column("Title")
    for e in entries:
        table.add_row(
            e.spec.id,
            e.state.status.value,
            str(e.state.attempts),
            e.state.last_finished_at.isoformat() if e.state.last_finished_at else "-",
            (e.state.session_id or "-")[:12],
            e.spec.title,
        )
    console.print(table)

    source = _build_source(settings)
    budget = TokenBudgetController(settings, source=source)
    budget.refresh()
    report = budget.report()
    console.print(
        f"\n5h window: used {report.used_5h:,} / {report.budget_5h:,} tokens  "
        f"(source: {report.source})"
    )
    console.print(
        f"Weekly:    used {report.used_week:,} / {report.budget_week:,} tokens  "
        f"target concurrency: {report.target_concurrency}"
    )
    return 0


def _cmd_resume(args: argparse.Namespace) -> int:
    project_dir: Path = args.project_dir.resolve()
    settings = load_settings(project_dir)
    state_store = StateStore(project_dir / settings.state_subdir)
    state = state_store.load(args.task_id)
    from claude_runner.models import TaskStatus

    if state.status == TaskStatus.COMPLETED or state.status in {
        TaskStatus.FAILED,
        TaskStatus.INTERRUPTED,
        TaskStatus.BLOCKED,
    }:
        state.status = TaskStatus.PENDING
    state.error = None
    state_store.save(state)
    Console().print(f"[bold green]Requeued[/] {args.task_id}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    project_dir: Path = args.project_dir.resolve()
    settings = load_settings(project_dir)
    todo_dir = project_dir / settings.todo_subdir
    state_store = StateStore(project_dir / settings.state_subdir)

    # Re-queue any tasks that were running/interrupted from a prior invocation.
    from claude_runner.models import TaskStatus

    for st in state_store.iter_states():
        if st.status in {TaskStatus.RUNNING, TaskStatus.INTERRUPTED, TaskStatus.QUEUED}:
            st.status = TaskStatus.INTERRUPTED if st.session_id else TaskStatus.PENDING
            state_store.save(st)

    emitter = EventEmitter(
        events_path=state_store.events_path(),
        log_dir=state_store.root / "logs",
    )
    catalog = TodoCatalog(todo_dir, state_store=state_store, settings=settings)
    source = _build_source(settings)
    budget = TokenBudgetController(settings, source=source)
    backend = _build_backend(settings, state_store=state_store, emitter=emitter)
    breaker = CircuitBreaker(
        max_consecutive_failures=settings.max_consecutive_failures,
        failure_rate_threshold=settings.failure_rate_threshold,
        rolling_window=settings.failure_rolling_window,
        min_samples=settings.failure_rate_min_samples,
    )

    scheduler = Scheduler(
        settings=settings,
        catalog=catalog,
        backend=backend,
        budget=budget,
        state_store=state_store,
        emitter=emitter,
        breaker=breaker,
    )

    try:
        outcome = asyncio.run(scheduler.run())
    except KeyboardInterrupt:
        emitter.emit("interrupted")
        Console().print("[yellow]interrupted[/]")
        return 130

    Console().print(
        f"completed={outcome.completed} failed={outcome.failed} "
        f"breaker_tripped={outcome.breaker_tripped}"
    )
    if outcome.breaker_tripped:
        Console().print(f"[red]circuit breaker:[/] {outcome.breaker_reason}")
        return 2
    if outcome.failed > 0:
        return 1
    return 0


# ----- wiring ----------------------------------------------------------


def _build_source(settings: Settings) -> BudgetSource | None:
    if settings.budget_source == "ccusage":
        src = CCUsageSource()
        if src.available():
            return src
        _log.warning("ccusage not on PATH; falling back to claude /context")
        return ContextCmdSource()
    if settings.budget_source == "context":
        return ContextCmdSource()
    if settings.budget_source == "api_headers":
        return ApiHeadersSource(
            itpm_budget=settings.resolved_budget_5h(),
            weekly_budget=settings.resolved_budget_weekly(),
        )
    return None  # "static"


def _build_backend(
    settings: Settings, *, state_store: StateStore, emitter: EventEmitter
) -> RunnerBackend:
    if settings.backend == "subprocess":
        return SubprocessBackend(state_store=state_store, emitter=emitter)
    return AsyncioBackend(state_store=state_store, emitter=emitter)


if __name__ == "__main__":
    sys.exit(main())
