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
from claude_runner.budget.sources.claude_usage import ClaudeUsageSource
from claude_runner.budget.sources.context_cmd import ContextCmdSource
from claude_runner.config import Settings, load_settings
from claude_runner.defaults import Effort
from claude_runner.notify.emitter import EventEmitter
from claude_runner.runner.asyncio_backend import AsyncioBackend
from claude_runner.runner.backend import RunnerBackend
from claude_runner.runner.scheduler import Scheduler
from claude_runner.runner.subprocess_backend import SubprocessBackend
from claude_runner.scaffold import init_project, write_new_task
from claude_runner.sidecar.schema import Answer, InteractionResponse, RequestState
from claude_runner.sidecar.store import SidecarStore, SidecarValidationError
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
    p_status.add_argument(
        "--active",
        action="store_true",
        help=(
            "Show only non-terminal tasks (running, ready_to_resume, "
            "awaiting_input, pending, queued, failed). Hides completed "
            "tasks — much more scannable on large queues."
        ),
    )
    p_status.add_argument(
        "--filter",
        dest="status_filter",
        default=None,
        help=(
            "Comma-separated list of statuses to show "
            "(e.g. `running,awaiting_input`). Overrides --active."
        ),
    )
    p_status.add_argument(
        "--compact",
        "-c",
        action="store_true",
        help=(
            "Skip the per-task table entirely; print only the per-status "
            "count line + budget summary + any awaiting-input alerts. "
            "Good for scripts and quick at-a-glance checks."
        ),
    )

    p_awaiting = sub.add_parser(
        "awaiting",
        help=(
            "List or inspect open sidecar stop-and-ask requests. "
            "Without arguments, lists all tasks awaiting input. "
            "With --show, pretty-prints a single task's request."
        ),
    )
    p_awaiting.add_argument("project_dir", nargs="?", default=".", type=Path)
    p_awaiting.add_argument(
        "--show",
        dest="show_task_id",
        default=None,
        help="Task id whose open request should be pretty-printed in full.",
    )

    p_validate = sub.add_parser("validate", help="Parse every task YAML and report errors")
    p_validate.add_argument("project_dir", nargs="?", default=".", type=Path)

    p_resume = sub.add_parser("resume", help="Mark a single task for re-run")
    p_resume.add_argument("task_id")
    p_resume.add_argument("project_dir", nargs="?", default=".", type=Path)

    p_input = sub.add_parser(
        "input",
        help="Answer a task that is AWAITING_INPUT (sidecar stop-and-ask)",
    )
    p_input.add_argument("task_id")
    p_input.add_argument("project_dir", nargs="?", default=".", type=Path)
    p_input.add_argument(
        "--answers",
        default=None,
        help='Inline JSON mapping question id -> answer, e.g. \'{"source":"C"}\'',
    )
    p_input.add_argument(
        "--from-file",
        dest="from_file",
        type=Path,
        default=None,
        help="Path to a JSON file containing {question_id: answer, ...}",
    )
    p_input.add_argument("--notes", default=None, help="Free-text notes to attach to the response")
    p_input.add_argument(
        "--cancel",
        action="store_true",
        help="Cancel the open request instead of answering (fails the task)",
    )

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
    if args.command == "input":
        return _cmd_input(args)
    if args.command == "awaiting":
        return _cmd_awaiting(args)
    parser.error(f"unknown command {args.command}")
    raise AssertionError  # pragma: no cover - parser.error always raises SystemExit


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


_ACTIVE_STATUSES = {
    "running",
    "queued",
    "ready_to_resume",
    "awaiting_input",
    "pending",
    "failed",
}


def _resolve_status_filter(args: argparse.Namespace) -> set[str] | None:
    """Figure out which TaskStatus values the operator wants to see.

    Precedence: ``--filter`` (explicit list) > ``--active`` (hide completed) >
    None (show everything). Returns the set of allowed status strings, or
    ``None`` for "no filter".
    """
    raw = getattr(args, "status_filter", None)
    if raw:
        return {s.strip().lower() for s in raw.split(",") if s.strip()}
    if getattr(args, "active", False):
        return set(_ACTIVE_STATUSES)
    return None


def _cmd_status(args: argparse.Namespace) -> int:
    project_dir: Path = args.project_dir.resolve()
    settings = load_settings(project_dir)
    runner_root = project_dir / settings.state_subdir
    state_store = StateStore(runner_root)
    sidecar_store = SidecarStore(runner_root / "sidecar")
    catalog = TodoCatalog(
        project_dir / settings.todo_subdir, state_store=state_store, settings=settings
    )
    entries = catalog.all_entries()
    console = Console()

    # Always compute the per-status count line — it's scannable and cheap.
    from collections import Counter

    counts = Counter(e.state.status.value for e in entries)
    summary_parts = [f"{v} {k}" for k, v in sorted(counts.items())]
    console.print(f"[bold]{len(entries)} tasks:[/] " + ", ".join(summary_parts))

    status_filter = _resolve_status_filter(args)
    if status_filter is not None:
        filtered = [e for e in entries if e.state.status.value in status_filter]
    else:
        filtered = entries

    # Table is suppressed in compact mode or when the filter matches nothing.
    if not args.compact and filtered:
        title = f"claude_runner status — {project_dir}"
        if status_filter is not None:
            title += f" ({', '.join(sorted(status_filter))})"
        table = Table(title=title)
        table.add_column("ID")
        table.add_column("Status")
        table.add_column("Attempts", justify="right")
        table.add_column("Last run")
        table.add_column("Session")
        table.add_column("Title")
        for e in filtered:
            table.add_row(
                e.spec.id,
                e.state.status.value,
                str(e.state.attempts),
                e.state.last_finished_at.isoformat() if e.state.last_finished_at else "-",
                (e.state.session_id or "-")[:12],
                e.spec.title,
            )
        console.print(table)
    elif not args.compact and status_filter is not None:
        console.print(f"  [dim](no tasks match filter {sorted(status_filter)})[/]")

    # Surface any AWAITING_INPUT tasks so the operator sees pending questions.
    awaiting = [e for e in entries if e.state.is_awaiting_input()]
    if awaiting:
        console.print(
            "\n[bold yellow]Tasks awaiting operator input (use `claude-runner input`):[/]"
        )
        for e in awaiting:
            req = sidecar_store.find_open_request(e.spec.id)
            summary = req.summary if req is not None else "(request unreadable)"
            seq = req.sequence if req is not None else "?"
            console.print(f"  [yellow]•[/] {e.spec.id} (seq {seq}): {summary}")

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
    cal = budget.calibration
    if cal is not None:
        console.print(
            f"Plan=auto: budgets calibrated from {cal.source} "
            f"({cal.n_blocks} blocks, {cal.n_weeks} weeks). {cal.reason}."
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


def _cmd_input(args: argparse.Namespace) -> int:
    """Answer a task's sidecar stop-and-ask request.

    Reads the task's newest OPEN sidecar request, validates the operator's
    answers against the questions in that request, writes a matching
    ``response-<seq>.json`` atomically, and flips the task from
    ``AWAITING_INPUT`` to ``READY_TO_RESUME`` so the next scheduler tick
    dispatches it. Passing ``--cancel`` instead marks the task FAILED.
    """
    import json as _json
    from datetime import UTC, datetime

    from claude_runner.models import TaskStatus

    project_dir: Path = args.project_dir.resolve()
    settings = load_settings(project_dir)
    runner_root = project_dir / settings.state_subdir
    state_store = StateStore(runner_root)
    sidecar_store = SidecarStore(runner_root / "sidecar")

    console = Console()
    task_id: str = args.task_id

    open_req = sidecar_store.find_open_request(task_id)
    if open_req is None:
        console.print(
            f"[red]no open sidecar request found for task[/] {task_id} "
            f"under {sidecar_store.task_dir(task_id)}"
        )
        return 2

    # Cancel path: mark request + response CANCELLED, task FAILED.
    if args.cancel:
        sidecar_store.cancel_request(task_id, open_req.sequence, notes=args.notes)
        state = state_store.load(task_id)
        state.status = TaskStatus.FAILED
        state.error = args.notes or "operator cancelled sidecar request"
        state_store.save(state)
        console.print(
            f"[yellow]cancelled[/] sidecar request seq={open_req.sequence} "
            f"for task {task_id}; task marked FAILED"
        )
        return 0

    # Gather answers from --answers or --from-file.
    answers_raw: object
    if args.answers is not None and args.from_file is not None:
        console.print("[red]--answers and --from-file are mutually exclusive[/]")
        return 2
    if args.answers is not None:
        try:
            answers_raw = _json.loads(args.answers)
        except _json.JSONDecodeError as exc:
            console.print(f"[red]--answers is not valid JSON:[/] {exc}")
            return 2
    elif args.from_file is not None:
        try:
            with Path(args.from_file).open("r", encoding="utf-8") as fh:
                answers_raw = _json.load(fh)
        except (OSError, _json.JSONDecodeError) as exc:
            console.print(f"[red]could not read --from-file[/] {args.from_file}: {exc}")
            return 2
    else:
        console.print(
            "[red]must supply either --answers '<json>' or --from-file <path> (or --cancel)[/]"
        )
        return 2

    if not isinstance(answers_raw, dict):
        console.print(
            f"[red]answers payload must be a JSON object mapping question id -> value[/]; "
            f"got {type(answers_raw).__name__}"
        )
        return 2

    # Validate answer ids before writing anything.
    known_ids = open_req.question_ids()
    unknown = [k for k in answers_raw if k not in known_ids]
    if unknown:
        console.print(
            f"[red]unknown question id(s):[/] {sorted(unknown)}  (known: {sorted(known_ids)})"
        )
        return 2
    missing = [q.id for q in open_req.questions if q.id not in answers_raw]
    if missing:
        console.print(f"[red]missing answers for question id(s):[/] {sorted(missing)}")
        return 2

    answers = [Answer(id=str(k), value=v) for k, v in answers_raw.items()]
    response = InteractionResponse(
        task_id=task_id,
        sequence=open_req.sequence,
        responded_at=datetime.now(tz=UTC),
        answers=answers,
        state=RequestState.ANSWERED,
        notes=args.notes,
    )
    try:
        sidecar_store.write_response(response, request=open_req)
    except SidecarValidationError as exc:
        console.print(f"[red]sidecar validation error:[/] {exc}")
        return 2

    # Flip task status. The scheduler's own promotion loop would do this on
    # its next tick too, but we prefer an immediate flip so ``status`` shows
    # the new state right after ``input`` returns.
    state = state_store.load(task_id)
    if state.status is TaskStatus.AWAITING_INPUT:
        state.status = TaskStatus.READY_TO_RESUME
        state.error = None
        state_store.save(state)

    console.print(
        f"[bold green]Answered[/] task {task_id} seq={open_req.sequence}; "
        f"status -> {state.status.value}"
    )
    return 0


def _cmd_awaiting(args: argparse.Namespace) -> int:
    """List or inspect open sidecar stop-and-ask requests.

    Without ``--show``, prints a scannable list of every task that
    currently has an OPEN sidecar request along with its summary and
    request sequence — quicker than piping ``status`` through ``grep`` or
    hunting through ``.claude_runner/sidecar/`` by hand.

    With ``--show <task_id>``, pretty-prints the full newest OPEN
    request for that task (context, each question, each option, and the
    recommended answer) so the operator can decide without opening JSON
    manually.
    """
    project_dir: Path = args.project_dir.resolve()
    settings = load_settings(project_dir)
    runner_root = project_dir / settings.state_subdir
    sidecar_store = SidecarStore(runner_root / "sidecar")

    console = Console()

    if args.show_task_id:
        task_id: str = args.show_task_id
        req = sidecar_store.find_open_request(task_id)
        if req is None:
            console.print(f"[red]No open sidecar request for task[/] {task_id}")
            return 1
        console.print(
            f"[bold cyan]Task {task_id} — request seq {req.sequence}[/] (state: {req.state.value})"
        )
        console.print(f"[dim]created_at:[/] {req.created_at.isoformat()}")
        console.print(f"[bold]Summary:[/] {req.summary}")
        if req.context:
            console.print(f"[bold]Context:[/]\n{req.context}\n")
        for q in req.questions:
            console.print(f"\n[bold yellow]Q {q.id}:[/] {q.prompt}")
            for opt in q.options:
                marker = "[green][RECOMMENDED][/] " if q.recommended == opt.value else ""
                console.print(f"  {marker}[{opt.value}] {opt.label}")
            if q.recommended:
                console.print(f"  [dim]recommended:[/] {q.recommended}")
            if q.multi_select:
                console.print("  [dim](multi-select)[/]")
        console.print(
            "\n[dim]Answer with:[/] "
            f"claude-runner input {task_id} --answers '{{...}}' [--notes ...]"
        )
        return 0

    # List mode.
    task_ids = sorted(sidecar_store.list_awaiting_task_ids())
    if not task_ids:
        console.print("[green]No tasks awaiting operator input.[/]")
        return 0
    console.print(f"[bold]Tasks awaiting operator input ({len(task_ids)}):[/]")
    for task_id in task_ids:
        req = sidecar_store.find_open_request(task_id)
        if req is None:
            console.print(f"  [yellow]•[/] {task_id} (request unreadable)")
            continue
        summary = req.summary or "(no summary)"
        console.print(f"  [yellow]•[/] {task_id} [dim](seq {req.sequence})[/] — {summary}")
    console.print("\n[dim]Inspect one with:[/] claude-runner awaiting --show <task_id>")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    project_dir: Path = args.project_dir.resolve()
    settings = load_settings(project_dir)
    todo_dir = project_dir / settings.todo_subdir
    runner_root = project_dir / settings.state_subdir
    state_store = StateStore(runner_root)
    sidecar_store = SidecarStore(runner_root / "sidecar")

    # Re-queue any tasks that were running/interrupted from a prior invocation.
    # Preserve AWAITING_INPUT and READY_TO_RESUME across restarts.
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
    backend = _build_backend(
        settings,
        state_store=state_store,
        emitter=emitter,
        sidecar_store=sidecar_store,
    )
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
        sidecar_store=sidecar_store,
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
        _log.warning(
            "ccusage not available (no ccusage binary and no npx); falling back to claude /context"
        )
        return ContextCmdSource()
    if settings.budget_source == "claude_usage":
        cu = ClaudeUsageSource(
            budget_5h_tokens=settings.resolved_budget_5h(),
            budget_weekly_tokens=settings.resolved_budget_weekly(),
        )
        if cu.available():
            return cu
        _log.warning(
            "claude-usage helper not found on PATH; falling back to ccusage. "
            "Install the helper at ~/.local/bin/claude-usage or change budget_source."
        )
        fallback = CCUsageSource()
        if fallback.available():
            return fallback
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
    settings: Settings,
    *,
    state_store: StateStore,
    emitter: EventEmitter,
    sidecar_store: SidecarStore | None = None,
) -> RunnerBackend:
    if settings.backend == "subprocess":
        return SubprocessBackend(
            state_store=state_store, emitter=emitter, sidecar_store=sidecar_store
        )
    return AsyncioBackend(state_store=state_store, emitter=emitter, sidecar_store=sidecar_store)


if __name__ == "__main__":  # pragma: no cover - module-as-script, exercised via __main__.py
    sys.exit(main())
