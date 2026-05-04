"""``claude-task-runner queue`` — CLI surface skills (and operators) consume.

Skills do **not** import the Python schema directly. They invoke
``claude-task-runner queue list --json`` (etc.) and parse JSON. That
keeps the skill's Markdown brief and the boundary stable.

Subcommands:

* ``queue list``       — list pending tasks (``todo/`` YAMLs).
* ``queue states``     — list TaskState YAMLs, optionally filtered by status.
* ``queue show ID``    — show one task's input + state + runs.
* ``queue add``        — write a new Task YAML from CLI args.
"""

from __future__ import annotations

import json as _json
import re
import sys
from pathlib import Path

import typer
from rich.console import Console

from claude_task_runner.config.loader import load_settings
from claude_task_runner.queue.schema import Task
from claude_task_runner.queue.store import (
    QueueIOError,
    QueueSchemaError,
    list_pending_tasks,
    list_state_files,
    load_state,
    load_task,
    state_path_for,
    task_path_for,
    write_task_atomic,
)
from claude_task_runner.runner.effort_levels import (
    UnknownEffortLevel,
    UnknownModel,
    validate_effort,
)

app = typer.Typer(no_args_is_help=True)


_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
"""Allowed task-ID characters. Restrictive so the id can be safely used
as a filename without shell-escaping concerns."""


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
) -> None:
    """List pending tasks in ``<queue>/todo/`` (Task YAMLs)."""
    console = Console()
    out: list[dict[str, object]] = []
    for path in list_pending_tasks(queue_dir.resolve()):
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
        console.print(f"[bold]{item['id']}[/]  [dim]({item['model']}, {item['effort']})[/]")
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
        console.print(
            f"  model: {task_payload.get('model')}  effort: {task_payload.get('effort')}"
        )
    if "task_error" in payload:
        console.print(f"  [red]task error:[/] {payload['task_error']}")
    state_payload = payload.get("state")
    if isinstance(state_payload, dict):
        console.print(
            f"  status: {state_payload.get('status')}  "
            f"attempts: {state_payload.get('attempts')}"
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

    task = Task(
        id=task_id,
        title=title,
        prompt=prompt_text or "",
        model=model,
        effort=effort,
        priority=priority,
        allowed_tools=list(allowed_tools),
        tags=list(tag),
        weekly_critical=weekly_critical,
    )

    try:
        write_task_atomic(task, target)
    except QueueIOError as exc:
        console.print(f"[bold red]write failed:[/] {exc}")
        raise typer.Exit(code=2) from exc

    console.print(f"[green]wrote {target}[/]")
    sys.stdout.flush()
