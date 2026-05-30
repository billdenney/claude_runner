"""``claude-task-runner queue`` — CLI surface skills (and operators) consume.

Skills do **not** import the Python schema directly. They invoke
``claude-task-runner queue list --json`` (etc.) and parse JSON. That
keeps the skill's Markdown brief and the boundary stable.

Subcommands:

* ``queue list``                  — list pending tasks (``todo/`` YAMLs).
* ``queue states``                — list TaskState YAMLs, optionally filtered by status.
* ``queue show ID``               — show one task's input + state + runs.
* ``queue add``                   — write a new Task YAML from CLI args.
* ``queue backfill-working-dir``  — populate ``working_dir`` on existing
                                    null-valued YAMLs from the per-queue
                                    template (ADR-0023).
* ``queue restart-fresh``         — abandon a task's current claude session
                                    so the next dispatch starts on any
                                    available account (ADR-0024 escape hatch).
* ``queue force-dispatch``        — bypass throttle gates and dispatch one task now.
"""

from __future__ import annotations

import json as _json
import re
import sys
import time
from pathlib import Path

import typer
from rich.console import Console

from claude_task_runner.clock import RealClock
from claude_task_runner.config.loader import load_settings
from claude_task_runner.queue.schema import Task
from claude_task_runner.queue.store import (
    QueueIOError,
    QueueSchemaError,
    list_pending_tasks,
    list_state_files,
    load_state,
    load_task,
    queue_runtime_dir,
    state_path_for,
    task_path_for,
    write_state_atomic,
    write_task_atomic,
)
from claude_task_runner.runner import force_dispatch as fd_mod
from claude_task_runner.runner.effort_levels import (
    UnknownEffortLevel,
    UnknownModel,
    validate_effort,
)
from claude_task_runner.supervisor import pidfile as pidfile_mod

app = typer.Typer(no_args_is_help=True)


_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
"""Allowed task-ID characters. Restrictive so the id can be safely used
as a filename without shell-escaping concerns."""


class WorkingDirTemplateError(ValueError):
    """A ``[queue].working_dir_template`` substitution failed.

    Raised when the template references a placeholder other than
    ``{task_id}`` — e.g. a typo like ``{taskid}``, or a stray
    ``{queue_dir}`` left over from a future extension. Surfacing the
    error at template-application time (not at config load) keeps the
    schema permissive while still failing fast when an operator runs
    ``queue add``.
    """


def _apply_working_dir_template(template: str, task_id: str) -> Path | None:
    """Substitute ``{task_id}`` in ``template`` and return a :class:`Path`.

    Empty / whitespace-only template → ``None`` (caller should treat as
    "no template configured" and leave ``working_dir`` null). Unknown
    placeholders raise :class:`WorkingDirTemplateError`.
    """
    stripped = template.strip()
    if not stripped:
        return None
    try:
        rendered = stripped.format(task_id=task_id)
    except KeyError as exc:
        # str.format raises KeyError on the missing placeholder name.
        raise WorkingDirTemplateError(
            f"[queue].working_dir_template={template!r} references unknown "
            f"placeholder {exc.args[0]!r}; only {{task_id}} is supported"
        ) from exc
    except (IndexError, ValueError) as exc:
        raise WorkingDirTemplateError(
            f"[queue].working_dir_template={template!r} is not a valid format string: {exc}"
        ) from exc
    return Path(rendered)


def _emit(payload: object, *, json: bool, console: Console) -> None:
    """Emit ``payload`` as JSON or as a human-readable rich rendering.

    Skills always pass ``--json``; operators run interactively without.
    """
    if json:
        print(_json.dumps(payload, default=str, indent=2))
        return
    console.print(payload)


def _safe_load_state(path: Path) -> dict[str, object] | None:
    try:
        return load_state(path).model_dump(mode="json")
    except (QueueIOError, QueueSchemaError):
        return None


@app.command("list")
def list_tasks(
    *,
    queue_dir: Path = typer.Option(Path.cwd, "--queue", help="Queue directory."),
    json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
    order_by_dispatch: bool = typer.Option(
        False,
        "--order-by-dispatch",
        help=(
            "Sort tasks in the order the supervisor will dispatch them "
            "(priority high→normal→low, then task id). Default is filename order."
        ),
    ),
) -> None:
    """List pending tasks in ``<queue>/todo/`` (Task YAMLs)."""
    from claude_task_runner.runner.orchestrator import (
        planned_dispatch_order,
        priority_sort_key,
    )

    console = Console()
    out: list[dict[str, object]] = []
    qd = queue_dir.resolve()

    if order_by_dispatch:
        ordered_tasks = planned_dispatch_order(qd)
        # Surface unparseable YAMLs separately so the JSON consumer sees them.
        ordered_ids = {t.id for t in ordered_tasks}
        for path in list_pending_tasks(qd):
            if path.stem in ordered_ids:
                continue
            try:
                load_task(path)
            except (QueueIOError, QueueSchemaError) as exc:
                out.append({"id": path.stem, "path": str(path), "error": str(exc)})
        for rank, task in enumerate(ordered_tasks, start=1):
            path = qd / "todo" / f"{task.id}.yaml"
            out.append(
                {
                    "id": task.id,
                    "title": task.title,
                    "model": task.model,
                    "effort": task.effort,
                    "priority": task.priority,
                    "dispatch_rank": rank,
                    "sort_key": list(priority_sort_key(task)),
                    "weekly_critical": task.weekly_critical,
                    "weekly_deferrable": task.weekly_deferrable,
                    "tags": task.tags,
                    "depends_on": task.depends_on,
                    "path": str(path),
                }
            )
    else:
        for path in list_pending_tasks(qd):
            try:
                task = load_task(path)
            except (QueueIOError, QueueSchemaError) as exc:
                out.append(
                    {
                        "id": path.stem,
                        "path": str(path),
                        "error": str(exc),
                    }
                )
                continue
            out.append(
                {
                    "id": task.id,
                    "title": task.title,
                    "model": task.model,
                    "effort": task.effort,
                    "priority": task.priority,
                    "weekly_critical": task.weekly_critical,
                    "weekly_deferrable": task.weekly_deferrable,
                    "tags": task.tags,
                    "depends_on": task.depends_on,
                    "path": str(path),
                }
            )

    if json:
        print(_json.dumps({"tasks": out}, default=str, indent=2))
        return

    if not out:
        console.print("[dim]No pending tasks in todo/.[/]")
        return
    for item in out:
        if "error" in item:
            console.print(f"[red]{item['id']}[/]: {item['error']}")
            continue
        rank_prefix = f"[dim]#{item['dispatch_rank']:>3}[/] " if "dispatch_rank" in item else ""
        console.print(
            f"{rank_prefix}[bold]{item['id']}[/]  "
            f"[dim]({item['model']}, {item['effort']}, "
            f"priority={item['priority']})[/]"
        )
        console.print(f"  {item['title']}")


@app.command("states")
def list_states(
    *,
    queue_dir: Path = typer.Option(Path.cwd, "--queue", help="Queue directory."),
    status_filter: list[str] = typer.Option(
        [],
        "--status",
        help="Only emit tasks whose status is in this list.",
    ),
    json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """List TaskState YAMLs, optionally filtered by status.

    Skills use ``--status awaiting_sidecar`` to find work that needs
    operator attention, ``--status running`` for in-flight, etc.
    """
    console = Console()
    filter_set = set(status_filter)
    out: list[dict[str, object]] = []
    for path in list_state_files(queue_dir.resolve()):
        payload = _safe_load_state(path)
        if payload is None:
            out.append({"id": path.stem, "path": str(path), "error": "unparseable"})
            continue
        if filter_set and payload.get("status") not in filter_set:
            continue
        # Trim noisy fields when not in JSON mode for human readability.
        if not json:
            payload = {
                k: v
                for k, v in payload.items()
                if k
                in (
                    "task_id",
                    "status",
                    "attempts",
                    "session_id",
                    "last_started_at",
                    "last_finished_at",
                    "stop_reason",
                    "error",
                )
            }
        out.append(payload)

    if json:
        print(_json.dumps({"states": out}, default=str, indent=2))
        return

    if not out:
        console.print("[dim](no matching states)[/]")
        return
    for item in out:
        status = item.get("status", "?")
        color = (
            "green"
            if status == "completed"
            else "red"
            if status in ("failed", "failed_circuit_breaker")
            else "yellow"
        )
        console.print(f"[bold]{item.get('task_id', item.get('id', '?'))}[/]  [{color}]{status}[/]")
        if item.get("error"):
            console.print(f"  [dim]error:[/] {item['error']}")


@app.command("show")
def show_task(
    task_id: str = typer.Argument(..., help="Task ID (filename stem)."),
    *,
    queue_dir: Path = typer.Option(Path.cwd, "--queue", help="Queue directory."),
    json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Show the input YAML AND state YAML for one task."""
    qd = queue_dir.resolve()
    task_path = task_path_for(qd, task_id)
    state_path = state_path_for(qd, task_id)

    payload: dict[str, object] = {"task_id": task_id}
    try:
        if task_path.exists():
            payload["task"] = load_task(task_path).model_dump(mode="json")
        else:
            payload["task"] = None
    except (QueueIOError, QueueSchemaError) as exc:
        payload["task_error"] = str(exc)

    try:
        if state_path.exists():
            payload["state"] = load_state(state_path).model_dump(mode="json")
        else:
            payload["state"] = None
    except (QueueIOError, QueueSchemaError) as exc:
        payload["state_error"] = str(exc)

    if json:
        print(_json.dumps(payload, default=str, indent=2))
        return

    console = Console()
    console.print(f"[bold]{task_id}[/]")
    task_payload = payload.get("task")
    if isinstance(task_payload, dict):
        console.print(f"  title: {task_payload.get('title')}")
        console.print(f"  model: {task_payload.get('model')}  effort: {task_payload.get('effort')}")
    if "task_error" in payload:
        console.print(f"  [red]task error:[/] {payload['task_error']}")
    state_payload = payload.get("state")
    if isinstance(state_payload, dict):
        console.print(
            f"  status: {state_payload.get('status')}  attempts: {state_payload.get('attempts')}"
        )
        console.print(f"  session_id: {state_payload.get('session_id')}")
    if "state_error" in payload:
        console.print(f"  [red]state error:[/] {payload['state_error']}")


@app.command("add")
def add_task(
    *,
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Per-queue claude_runner.toml."
    ),
    queue_dir: Path = typer.Option(Path.cwd, "--queue", help="Queue directory."),
    task_id: str = typer.Option(..., "--id", help="Task identifier."),
    title: str = typer.Option(..., "--title", help="Short task title."),
    prompt_file: Path | None = typer.Option(None, "--prompt-file", help="Read prompt from a file."),
    prompt: str | None = typer.Option(
        None,
        "--prompt",
        help="Inline prompt (use --prompt-file for long content).",
    ),
    model: str = typer.Option("claude-opus-4-7", "--model", help="Model identifier."),
    effort: str = typer.Option("medium", "--effort", help="Effort level (validated per model)."),
    priority: str = typer.Option("normal", "--priority", help="low | normal | high"),
    allowed_tools: list[str] = typer.Option(
        [], "--allowed-tool", help="Repeat for each tool. e.g. Read, Write."
    ),
    tag: list[str] = typer.Option([], "--tag", help="Free-form tag (repeat for multiple)."),
    weekly_critical: bool = typer.Option(
        False,
        "--weekly-critical",
        help="Dispatch first to ensure completion this week.",
    ),
    add_dir: list[Path] = typer.Option(
        [],
        "--add-dir",
        help=(
            "Absolute directory the dispatched claude subprocess should be "
            "allowed to read/write outside its cwd. Repeat for multiple dirs. "
            "The queue dir is always added automatically; use this only for "
            "extra paths (e.g. a sibling repo, a shared data tree)."
        ),
    ),
    working_dir: Path | None = typer.Option(
        None,
        "--working-dir",
        help=(
            "Explicit cwd the dispatched task runs in (and the value the "
            "pre-dispatch hook receives as $TASK_WORKING_DIR). Overrides "
            "any [queue].working_dir_template in the per-queue config. "
            "Use --no-working-dir to force null when a template is set "
            "but this particular task shouldn't have a working_dir."
        ),
    ),
    no_working_dir: bool = typer.Option(
        False,
        "--no-working-dir",
        help=(
            "Force working_dir to null even when [queue].working_dir_template "
            "is configured. Useful for tasks that legitimately don't need "
            "a worktree (e.g. queue-wide categorization shards)."
        ),
    ),
    overwrite: bool = typer.Option(
        False, "--overwrite", help="Allow overwriting an existing task YAML."
    ),
) -> None:
    """Add a new Task YAML to ``<queue>/todo/<id>.yaml``.

    Skills (``/runner-add-task``) drive this with operator answers.
    """
    settings = load_settings(config)
    console = Console()
    qd = queue_dir.resolve()

    if not _ID_RE.match(task_id):
        console.print(f"[bold red]invalid task id:[/] {task_id!r} (allowed: {_ID_RE.pattern})")
        raise typer.Exit(code=2)

    if priority not in ("low", "normal", "high"):
        console.print(f"[bold red]invalid priority:[/] {priority!r}")
        raise typer.Exit(code=2)

    if prompt is None and prompt_file is None:
        console.print("[bold red]must supply --prompt or --prompt-file[/]")
        raise typer.Exit(code=2)
    if prompt is not None and prompt_file is not None:
        console.print("[bold red]give either --prompt OR --prompt-file, not both[/]")
        raise typer.Exit(code=2)

    prompt_text = prompt
    if prompt_file is not None:
        try:
            prompt_text = prompt_file.read_text()
        except OSError as exc:
            console.print(f"[bold red]read failed:[/] {exc}")
            raise typer.Exit(code=2) from exc

    try:
        validate_effort(model, effort, settings.effort_levels)
    except UnknownEffortLevel as exc:
        console.print(f"[bold red]invalid effort:[/] {exc}")
        raise typer.Exit(code=2) from exc
    except UnknownModel as exc:
        console.print(f"[bold red]unknown model:[/] {exc}")
        raise typer.Exit(code=2) from exc

    target = task_path_for(qd, task_id)
    if target.exists() and not overwrite:
        console.print(f"[bold red]task already exists:[/] {target}")
        raise typer.Exit(code=2)

    # Validate each --add-dir: warn (don't fail) on missing entries so the
    # operator can still queue a task whose dependencies will materialize
    # later (e.g. a worktree the pre-dispatch hook creates).
    for d in add_dir:
        if not d.is_dir():
            console.print(f"[yellow]warning:[/] --add-dir {d} is not an existing directory")

    # Resolve the task's working_dir.
    #
    # Precedence (highest first):
    #   1. --no-working-dir       — force null even when a template is set.
    #   2. --working-dir <path>   — explicit value wins over the template.
    #   3. [queue].working_dir_template — substitute {task_id} and use.
    #   4. None                   — preserve historical behavior.
    #
    # Operators with a pre-dispatch hook that depends on working_dir (e.g.
    # the nlmixr2lib popPK ingestion worktree hook) configure the template
    # so each new task gets a sensible default without per-call typing.
    if no_working_dir and working_dir is not None:
        console.print("[bold red]give either --working-dir OR --no-working-dir, not both[/]")
        raise typer.Exit(code=2)

    if no_working_dir:
        resolved_working_dir: Path | None = None
    elif working_dir is not None:
        resolved_working_dir = working_dir
    else:
        try:
            resolved_working_dir = _apply_working_dir_template(
                settings.queue.working_dir_template, task_id
            )
        except WorkingDirTemplateError as exc:
            console.print(f"[bold red]{exc}[/]")
            raise typer.Exit(code=2) from exc

    task = Task(
        id=task_id,
        title=title,
        prompt=prompt_text or "",
        working_dir=resolved_working_dir,
        model=model,
        effort=effort,
        priority=priority,
        allowed_tools=list(allowed_tools),
        tags=list(tag),
        weekly_critical=weekly_critical,
        additional_dirs=list(add_dir),
    )

    try:
        write_task_atomic(task, target)
    except QueueIOError as exc:
        console.print(f"[bold red]write failed:[/] {exc}")
        raise typer.Exit(code=2) from exc

    console.print(f"[green]wrote {target}[/]")
    sys.stdout.flush()


@app.command("backfill-working-dir")
def backfill_working_dir(
    *,
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Per-queue claude_runner.toml."
    ),
    queue_dir: Path = typer.Option(Path.cwd, "--queue", help="Queue directory."),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Report what would change without writing.",
    ),
    json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Populate ``working_dir`` on tasks in ``todo/`` whose value is null.

    Idempotent: skips any task whose ``working_dir`` is already set
    (regardless of whether the current value matches the template).
    Reads the per-queue ``[queue].working_dir_template`` and substitutes
    ``{task_id}`` per task; refuses to run when the template is unset
    (there is nothing to apply).

    Authoring history: this command exists because operators repeatedly
    forgot to set ``working_dir`` on hand-edited tasks (or queued tasks
    before the template was configured) and the pre-dispatch hook then
    short-circuited at runtime, costing dispatch attempts. Backfilling
    upfront catches the omission once. See ADR-0023.
    """
    console = Console()
    qd = queue_dir.resolve()
    settings = load_settings(config)

    template = settings.queue.working_dir_template
    if not template.strip():
        msg = (
            "[queue].working_dir_template is not set; nothing to backfill. "
            "Configure it in the per-queue claude_runner.toml before running this command."
        )
        if json:
            print(_json.dumps({"ok": False, "error": msg}))
        else:
            console.print(f"[bold red]{msg}[/]")
        raise typer.Exit(code=2)

    updated: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []

    for path in list_pending_tasks(qd):
        try:
            task = load_task(path)
        except (QueueIOError, QueueSchemaError) as exc:
            errors.append({"id": path.stem, "path": str(path), "error": str(exc)})
            continue
        if task.working_dir is not None:
            skipped.append(
                {
                    "id": task.id,
                    "path": str(path),
                    "reason": "working_dir already set",
                    "working_dir": str(task.working_dir),
                }
            )
            continue
        try:
            new_dir = _apply_working_dir_template(template, task.id)
        except WorkingDirTemplateError as exc:
            errors.append({"id": task.id, "path": str(path), "error": str(exc)})
            continue
        if new_dir is None:
            # Template stripped to empty after substitution — treat as
            # "no value." We only get here if the template was non-empty
            # whitespace, which the outer guard already rejected, but
            # keep the branch defensive.
            skipped.append({"id": task.id, "path": str(path), "reason": "template rendered empty"})
            continue
        if dry_run:
            updated.append({"id": task.id, "path": str(path), "working_dir": str(new_dir)})
            continue
        new_task = task.model_copy(update={"working_dir": new_dir})
        try:
            write_task_atomic(new_task, path)
        except QueueIOError as exc:
            errors.append({"id": task.id, "path": str(path), "error": str(exc)})
            continue
        updated.append({"id": task.id, "path": str(path), "working_dir": str(new_dir)})

    payload = {
        "ok": not errors,
        "dry_run": dry_run,
        "template": template,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
    }
    if json:
        print(_json.dumps(payload, indent=2))
    else:
        verb = "would update" if dry_run else "updated"
        console.print(f"[green]{verb} {len(updated)} task(s)[/]; skipped {len(skipped)}")
        for u in updated:
            console.print(f"  [bold]{u['id']}[/]  -> {u['working_dir']}")
        if errors:
            console.print(f"[bold red]{len(errors)} error(s):[/]")
            for e in errors:
                console.print(f"  [red]{e['id']}[/]: {e['error']}")
    if errors:
        raise typer.Exit(code=1)


def _supervisor_is_alive(queue_dir: Path) -> bool:
    pid_path = queue_dir / ".claude_task_runner" / "supervisor.pid"
    pid = pidfile_mod.read_existing_pid(pid_path)
    return pid is not None and pidfile_mod.is_pid_alive(pid)


@app.command("restart-fresh")
def restart_fresh(
    task_id: str = typer.Argument(..., help="Task id whose session to abandon."),
    *,
    queue_dir: Path = typer.Option(Path.cwd, "--queue", help="Queue directory."),
    json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Clear a task's ``session_id`` so the next dispatch starts fresh.

    Escape hatch for ADR-0024 session affinity: when a task's affined
    account is stuck (weekly-throttled, paused, or removed from
    ``[[accounts]]``), the orchestrator refuses to dispatch the task on
    a different account because resuming the session under a different
    ``CLAUDE_CONFIG_DIR`` produces ``No conversation found with session
    ID: …``. ``restart-fresh`` nulls both ``session_id`` and
    ``session_account`` on the state YAML. The next dispatch picks
    whichever account ``choose_account`` selects normally, creates a
    fresh session, and continues from the original task prompt
    (cached context is lost — that's the trade-off).

    Exits non-zero if the task has no state YAML or the YAML cannot be
    parsed. Idempotent: a task without a session_id is left unchanged
    (the command reports ``noop=True`` so scripts can branch).
    """
    console = Console()
    qd = queue_dir.resolve()
    state_path = state_path_for(qd, task_id)
    if not state_path.exists():
        msg = f"no state YAML for task {task_id!r}: {state_path}"
        if json:
            print(_json.dumps({"ok": False, "error": msg}))
        else:
            console.print(f"[bold red]{msg}[/]")
        raise typer.Exit(code=2)
    try:
        state = load_state(state_path)
    except (QueueIOError, QueueSchemaError) as exc:
        msg = f"cannot parse state YAML {state_path}: {exc}"
        if json:
            print(_json.dumps({"ok": False, "error": msg}))
        else:
            console.print(f"[bold red]{msg}[/]")
        raise typer.Exit(code=2) from exc

    if state.session_id is None and state.session_account is None:
        if json:
            print(
                _json.dumps(
                    {
                        "ok": True,
                        "noop": True,
                        "task_id": task_id,
                        "reason": "no session to clear",
                    }
                )
            )
        else:
            console.print(f"[dim]task {task_id} has no active session; nothing to clear.[/]")
        return

    prior_session = state.session_id
    prior_account = state.session_account
    new_state = state.model_copy(
        update={
            "session_id": None,
            "session_account": None,
            "resume_attempts": 0,
        }
    )
    write_state_atomic(new_state, state_path)
    if json:
        print(
            _json.dumps(
                {
                    "ok": True,
                    "noop": False,
                    "task_id": task_id,
                    "cleared_session_id": prior_session,
                    "cleared_session_account": prior_account,
                }
            )
        )
    else:
        console.print(
            f"[green]cleared session for task {task_id}:[/] "
            f"session_id={prior_session!r}, session_account={prior_account!r}. "
            "Next dispatch starts fresh on any available account."
        )


@app.command("force-dispatch")
def force_dispatch(
    task_id: str = typer.Argument(..., help="Task id (filename stem) to dispatch."),
    *,
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Per-queue claude_runner.toml."
    ),
    queue_dir: Path = typer.Option(Path.cwd, "--queue", help="Queue directory."),
    over_limit: bool = typer.Option(
        False,
        "--over-limit",
        help=(
            "Allow exceeding max_concurrency for this one dispatch. Without "
            "this flag the supervisor waits for a free slot before honouring "
            "the request."
        ),
    ),
    wait_seconds: float = typer.Option(
        60.0,
        "--wait-seconds",
        help=(
            "When the supervisor is running, poll up to this many seconds for "
            "the task to enter `running` status before returning. 0 = don't wait."
        ),
    ),
    json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Bypass throttle and priority; dispatch ``task_id`` next.

    Use when an operator needs a single high-priority task to run NOW
    even though the supervisor is in ``throttled_5h`` or ``throttled_weekly``.

    Behavior depends on whether the supervisor is running:

    * **Supervisor running.** Writes a request file under
      ``<queue>/.claude_task_runner/force_dispatch/<task_id>.req``;
      the supervisor consumes it on the next tick (typically <30 s).
      Without ``--over-limit`` the supervisor declines if all
      ``max_concurrency`` slots are taken and the request file persists
      for a later tick. The pre-dispatch hook runs as normal.
    * **Supervisor not running.** Runs the dispatch in-process and
      blocks until the attempt finishes. ``--over-limit`` is implied
      (no in-flight slots to conflict with) and ignored.

    Exits non-zero if the task YAML is missing from ``todo/``, if its
    current status is not dispatchable (``running``,
    ``awaiting_sidecar``, ``completed``, ``failed_circuit_breaker``,
    ``weekly_paused``), or if the pre-dispatch hook fails during the
    synchronous path.
    """
    console = Console()
    qd = queue_dir.resolve()
    settings = load_settings(config)
    queue_runtime_dir(qd)

    task_path = task_path_for(qd, task_id)
    if not task_path.exists():
        msg = f"task YAML not in todo/: {task_path}"
        if json:
            print(_json.dumps({"ok": False, "error": msg}))
        else:
            console.print(f"[bold red]{msg}[/]")
        raise typer.Exit(code=2)
    try:
        load_task(task_path)
    except (QueueIOError, QueueSchemaError) as exc:
        msg = f"task YAML invalid: {exc}"
        if json:
            print(_json.dumps({"ok": False, "error": msg}))
        else:
            console.print(f"[bold red]{msg}[/]")
        raise typer.Exit(code=2) from exc

    state_path = state_path_for(qd, task_id)
    current_status: str | None = None
    if state_path.exists():
        try:
            current_status = load_state(state_path).status
        except (QueueIOError, QueueSchemaError):
            current_status = None
    if current_status not in (None, "pending", "failed"):
        msg = f"task {task_id} status={current_status!r} is not dispatchable"
        if json:
            print(_json.dumps({"ok": False, "error": msg, "status": current_status}))
        else:
            console.print(f"[bold red]{msg}[/]")
        raise typer.Exit(code=2)

    if _supervisor_is_alive(qd):
        path = fd_mod.write_request(qd, task_id, allow_over_limit=over_limit)
        if not json:
            console.print(
                f"[green]request written:[/] {path}\n"
                f"[dim]supervisor will pick it up on the next tick "
                f"(allow_over_limit={over_limit}).[/]"
            )
        picked_up = _poll_until_running(qd, task_id, wait_seconds)
        payload = {
            "ok": True,
            "mode": "supervised",
            "request_path": str(path),
            "running": picked_up,
            "allow_over_limit": over_limit,
        }
        if json:
            print(_json.dumps(payload))
        elif picked_up:
            console.print(f"[green]task {task_id} entered `running` status.[/]")
        elif wait_seconds > 0:
            console.print(
                f"[yellow]task {task_id} still not running after {wait_seconds}s — "
                "the supervisor may be honouring max_concurrency. The request "
                "file persists; the task will dispatch on the next free slot.[/]"
            )
        return

    # No supervisor running: do it inline. No race possible.
    try:
        new_state = fd_mod.dispatch_synchronously(
            task_id=task_id,
            queue_dir=qd,
            settings=settings,
            clock=RealClock(),
            claude_executable=settings.claude.executable,
        )
    except fd_mod.ForceDispatchError as exc:
        msg = str(exc)
        if json:
            print(_json.dumps({"ok": False, "error": msg}))
        else:
            console.print(f"[bold red]force-dispatch failed:[/] {msg}")
        raise typer.Exit(code=2) from exc

    if json:
        print(
            _json.dumps(
                {
                    "ok": True,
                    "mode": "synchronous",
                    "status": new_state.status,
                    "attempts": new_state.attempts,
                    "stop_reason": new_state.stop_reason,
                }
            )
        )
        return
    color = "green" if new_state.status == "completed" else "yellow"
    console.print(
        f"[{color}]task {task_id} finished with status={new_state.status}"
        f", attempts={new_state.attempts}, stop_reason={new_state.stop_reason}[/]"
    )


def _poll_until_running(queue_dir: Path, task_id: str, wait_seconds: float) -> bool:
    """Poll the state YAML for status='running'; return True when seen."""
    if wait_seconds <= 0:
        return False
    deadline = time.monotonic() + wait_seconds
    path = state_path_for(queue_dir, task_id)
    while time.monotonic() < deadline:
        if path.exists():
            try:
                status = load_state(path).status
            except (QueueIOError, QueueSchemaError):
                status = None
            if status == "running":
                return True
            # If it raced through "running" to "completed"/"failed", also accept.
            if status in ("completed", "failed", "awaiting_sidecar"):
                return True
        time.sleep(0.5)
    return False
