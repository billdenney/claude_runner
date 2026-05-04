"""``claude-task-runner sidecar`` — CLI surface the ``/runner-answer-sidecar``
skill drives.

The skill walks the operator through one open sidecar at a time using
:func:`AskUserQuestion` for click-only answering. This CLI provides:

* ``sidecar list``    — open sidecars (``--json``).
* ``sidecar show``    — one request's full content (``--json``).
* ``sidecar answer``  — write a response from a JSON file or string.

Keeping the CLI dumb means the skill (Markdown) stays small and the
heavy lifting stays here where it's tested.
"""

from __future__ import annotations

import json as _json
import sys
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console

from claude_task_runner.queue.schema import (
    SidecarAnswer,
    SidecarResponse,
)
from claude_task_runner.queue.sidecar import (
    list_open_sidecars,
    read_request,
    write_response,
)
from claude_task_runner.queue.store import QueueIOError, QueueSchemaError

app = typer.Typer(no_args_is_help=True)


@app.command("list")
def list_sidecars(
    *,
    queue_dir: Path = typer.Option(Path.cwd, "--queue", help="Queue directory."),
    json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """List unanswered sidecar requests across all tasks.

    JSON output is the contract the ``/runner-answer-sidecar`` skill
    relies on; humans get a one-line-per-sidecar summary.
    """
    qd = queue_dir.resolve()
    items: list[dict[str, object]] = []
    for task_id, sequence, request_path in list_open_sidecars(qd):
        try:
            req = read_request(request_path)
        except (QueueIOError, QueueSchemaError) as exc:
            items.append(
                {
                    "task_id": task_id,
                    "sequence": sequence,
                    "request_path": str(request_path),
                    "error": str(exc),
                }
            )
            continue
        items.append(
            {
                "task_id": task_id,
                "sequence": sequence,
                "request_path": str(request_path),
                "summary": req.summary,
                "questions": [q.id for q in req.questions],
                "created_at": req.created_at.isoformat(),
            }
        )

    if json:
        print(_json.dumps({"sidecars": items}, default=str, indent=2))
        return

    console = Console()
    if not items:
        console.print("[dim]No open sidecars.[/]")
        return
    for item in items:
        if "error" in item:
            console.print(f"[red]{item['task_id']}/{item['sequence']:>3d}[/] {item['error']}")
            continue
        console.print(
            f"[bold]{item['task_id']}[/] [dim]#{item['sequence']:03d}[/] {item['summary']}"
        )


@app.command("show")
def show_sidecar(
    task_id: str = typer.Argument(...),
    sequence: int = typer.Argument(..., min=1),
    *,
    queue_dir: Path = typer.Option(Path.cwd, "--queue", help="Queue directory."),
    json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Print the full content of one sidecar request."""
    from claude_task_runner.queue.sidecar import request_path

    qd = queue_dir.resolve()
    path = request_path(qd, task_id, sequence)
    if not path.exists():
        sys.stderr.write(f"sidecar not found: {path}\n")
        raise typer.Exit(code=1)
    try:
        req = read_request(path)
    except (QueueIOError, QueueSchemaError) as exc:
        sys.stderr.write(f"failed to read {path}: {exc}\n")
        raise typer.Exit(code=2) from exc

    payload = req.model_dump(mode="json")
    if json:
        print(_json.dumps(payload, default=str, indent=2))
        return

    console = Console()
    console.print(f"[bold]task:[/] {req.task_id}  [bold]seq:[/] {req.sequence}")
    console.print(f"[bold]summary:[/] {req.summary}")
    if req.context:
        console.print("[bold]context:[/]")
        for line in req.context.splitlines():
            console.print(f"  {line}")
    for q in req.questions:
        console.print(f"\n[bold cyan]Q[/] [bold]{q.id}[/]: {q.prompt}")
        for opt in q.options:
            marker = "*" if opt.value == q.recommended else " "
            console.print(f"  {marker} {opt.value}: {opt.label}")
            if opt.description:
                console.print(f"      [dim]{opt.description}[/]")
        if q.allow_free_text:
            console.print("  [dim](free-text answer permitted)[/]")
        if q.multi_select:
            console.print("  [dim](multi-select)[/]")


@app.command("answer")
def answer_sidecar(
    task_id: str = typer.Argument(...),
    sequence: int = typer.Argument(..., min=1),
    *,
    queue_dir: Path = typer.Option(Path.cwd, "--queue", help="Queue directory."),
    answers_json: str | None = typer.Option(
        None,
        "--answers",
        help=('Inline JSON array of {id, value} answers. Example: \'[{"id":"q1","value":"A"}]\''),
    ),
    answers_file: Path | None = typer.Option(
        None, "--answers-file", help="Read JSON answers from a file."
    ),
    notes: str = typer.Option("", "--notes", help="Free-text operator notes."),
) -> None:
    """Write a response to a sidecar request.

    ``--answers`` takes the same JSON shape as
    :class:`SidecarResponse.answers` (a list of ``{"id": str, "value":
    str | list[str]}`` objects). The ``/runner-answer-sidecar`` skill
    builds this list from operator clicks and invokes us.
    """
    if (answers_json is None) == (answers_file is None):
        sys.stderr.write("supply exactly one of --answers / --answers-file\n")
        raise typer.Exit(code=2)

    if answers_json is not None:
        text = answers_json
    else:
        assert answers_file is not None
        try:
            text = answers_file.read_text()
        except OSError as exc:
            sys.stderr.write(f"failed to read {answers_file}: {exc}\n")
            raise typer.Exit(code=2) from exc

    try:
        raw = _json.loads(text)
    except _json.JSONDecodeError as exc:
        sys.stderr.write(f"invalid JSON: {exc}\n")
        raise typer.Exit(code=2) from exc

    if not isinstance(raw, list):
        sys.stderr.write("answers must be a JSON array\n")
        raise typer.Exit(code=2)

    answers: list[SidecarAnswer] = []
    for entry in raw:
        if not isinstance(entry, dict) or "id" not in entry or "value" not in entry:
            sys.stderr.write("each answer must be an object with 'id' and 'value' keys\n")
            raise typer.Exit(code=2)
        answers.append(
            SidecarAnswer(
                id=str(entry["id"]),
                value=entry["value"],
            )
        )

    response = SidecarResponse(
        task_id=task_id,
        sequence=sequence,
        responded_at=datetime.now(UTC),
        answers=answers,
        notes=notes,
    )
    qd = queue_dir.resolve()
    path = write_response(qd, response)
    print(_json.dumps({"wrote": str(path), "task_id": task_id, "sequence": sequence}))
