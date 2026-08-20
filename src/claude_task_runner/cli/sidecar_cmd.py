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
from typing import cast

import typer
from pydantic import ValidationError
from rich.console import Console

from claude_task_runner.queue.schema import (
    SidecarAnswer,
    SidecarResponse,
)
from claude_task_runner.queue.sidecar import (
    answered_question_ids,
    load_sidecar_payload,
    open_sidecars,
    read_request,
    request_outstanding,
    response_path,
    write_response,
)
from claude_task_runner.queue.store import QueueIOError, QueueSchemaError

app = typer.Typer(no_args_is_help=True)

_SUMMARY_WIDTH = 100


def _one_line(summary: object) -> str:
    """First line of a summary, clipped, for the one-row-per-sidecar view.

    Sidecar summaries run to several paragraphs in practice. Printed in
    full across a listing of a hundred-odd open requests they bury the
    thing the listing exists to show. ``--json`` and ``sidecar show`` still
    carry the whole text.
    """
    if not isinstance(summary, str) or not summary:
        return ""
    first = summary.strip().splitlines()[0].strip()
    if len(first) <= _SUMMARY_WIDTH:
        return first if first == summary.strip() else first + " […]"
    return first[: _SUMMARY_WIDTH - 1].rstrip() + "…"


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
    for item in open_sidecars(qd):
        row: dict[str, object] = {
            "task_id": item.task_id,
            "sequence": item.sequence,
            "request_path": str(item.request_path),
            # The ids still owed an answer. An operator (or the answering
            # skill) needs these, not just the task id: on a partially
            # answered request the task id alone does not say what is
            # missing.
            "outstanding": list(item.outstanding),
            "answered": list(item.answered),
            "partial": item.partial,
            "response_path": str(item.response_path) if item.response_path else None,
        }
        if item.error is not None:
            row["error"] = item.error
            items.append(row)
            continue
        try:
            req = read_request(item.request_path)
        except (QueueIOError, QueueSchemaError) as exc:
            # The request is genuinely open (open_sidecars decided that from
            # its question ids); only the rich presentation fields are
            # unavailable, so keep the row and say why.
            row["schema_warning"] = str(exc)
            items.append(row)
            continue
        row["summary"] = req.summary
        row["questions"] = [q.id for q in req.questions]
        row["created_at"] = req.created_at.isoformat()
        row["prompts"] = {q.id: q.prompt for q in req.questions if q.id in set(item.outstanding)}
        # Names the request proposes as canonical, unioned across the
        # outstanding questions' options. Lets a queue-side triage script
        # collision-check mechanically instead of parsing prose.
        proposed: list[str] = []
        for q in req.questions:
            if q.id not in set(item.outstanding):
                continue
            for opt in q.options:
                for name in opt.proposed_names:
                    if name not in proposed:
                        proposed.append(name)
        row["proposed_names"] = proposed
        items.append(row)

    n_questions = sum(len(cast("list[str]", row["outstanding"])) for row in items)
    if json:
        print(
            _json.dumps(
                {
                    "sidecars": items,
                    "n_open": len(items),
                    "n_outstanding_questions": n_questions,
                },
                default=str,
                indent=2,
            )
        )
        return

    console = Console()
    if not items:
        console.print("[dim]No open sidecars.[/]")
        return
    for row in items:
        head = f"[bold]{row['task_id']}[/] [dim]#{row['sequence']:03d}[/]"
        if "error" in row:
            console.print(f"[red]{head} unreadable:[/] {row['error']}")
            continue
        outstanding = cast("list[str]", row["outstanding"])
        answered = cast("list[str]", row["answered"])
        # A response that credits none of the asked ids is a different fault
        # from a half-finished one -- usually an answer written against the
        # wrong id -- and saying "partial" for it would misdescribe it.
        if not row["partial"]:
            marker = ""
        elif answered:
            marker = "[yellow]partial[/] "
        else:
            marker = "[red]unmatched[/] "
        ids = ", ".join(outstanding) or "(no questions)"
        console.print(f"{head} {marker}[cyan]{ids}[/] {_one_line(row.get('summary'))}")
        if row["partial"] and answered:
            console.print(f"    [dim]already answered: {', '.join(answered)}[/]")
        elif row["partial"]:
            console.print("    [dim]a response exists but answers none of the asked ids[/]")
    console.print(
        f"\n[bold]{len(items)}[/] open request(s), [bold]{n_questions}[/] unanswered question(s)."
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
    merge: bool = typer.Option(
        False,
        "--merge",
        help=(
            "Carry forward answers already recorded in the existing response "
            "for question ids not supplied here, so answering only the "
            "outstanding questions still produces a complete response."
        ),
    ),
    allow_partial: bool = typer.Option(
        False,
        "--allow-partial",
        help=(
            "Write the response even though it does not answer every question "
            "the request asked (or the request cannot be read). The sidecar "
            "stays open on the omitted ids."
        ),
    ),
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
                notes=str(entry.get("notes", "")),
            )
        )

    qd = queue_dir.resolve()
    if merge:
        answers = _merge_existing_answers(qd, task_id, sequence, answers)
    supplied = [a.id for a in answers]
    _check_answers_cover_request(
        qd,
        task_id,
        sequence,
        supplied,
        allow_partial=allow_partial,
    )

    response = SidecarResponse(
        task_id=task_id,
        sequence=sequence,
        responded_at=datetime.now(UTC),
        answers=answers,
        notes=notes,
    )
    path = write_response(qd, response)
    print(_json.dumps({"wrote": str(path), "task_id": task_id, "sequence": sequence}))


def _merge_existing_answers(
    queue_dir: Path,
    task_id: str,
    sequence: int,
    answers: list[SidecarAnswer],
) -> list[SidecarAnswer]:
    """Append answers already on file for ids this call did not supply.

    ``answer`` rewrites the response file wholesale, so topping up a
    partially answered request would otherwise mean retyping the answers
    the operator already gave. Merging keeps the completeness gate
    satisfiable without inventing anything: every carried answer comes
    verbatim from the existing response.
    """
    existing = response_path(queue_dir, task_id, sequence)
    if not existing.exists():
        return answers
    try:
        payload = load_sidecar_payload(existing)
        prior = SidecarResponse.model_validate(payload).answers
    except (QueueIOError, QueueSchemaError, ValidationError) as exc:
        sys.stderr.write(f"warning: --merge could not read {existing.name}: {exc}\n")
        return answers
    supplied = {a.id for a in answers}
    carried = [a for a in prior if a.id not in supplied]
    if carried:
        sys.stderr.write(
            f"merged {len(carried)} existing answer(s) from {existing.name}: "
            f"{', '.join(a.id for a in carried)}\n"
        )
    return answers + carried


def _check_answers_cover_request(
    queue_dir: Path,
    task_id: str,
    sequence: int,
    supplied: list[str],
    *,
    allow_partial: bool,
) -> None:
    """Abort unless ``supplied`` answers every question the request asked.

    This is the half of the per-question fix that stops the gap being
    created. Detecting partial responses after the fact only tells the
    operator how far behind they already are; refusing to write one keeps
    the backlog from growing. The response file is rewritten wholesale on
    every ``answer`` call, so demanding full coverage also means a later
    call can never silently drop an earlier answer.

    ``--allow-partial`` is the deliberate escape hatch: the write proceeds
    and the sidecar stays open on the omitted ids.
    """
    try:
        _outstanding, asked, existing_response = request_outstanding(queue_dir, task_id, sequence)
    except (QueueIOError, QueueSchemaError) as exc:
        # Cannot tell what was asked -> cannot tell whether this response is
        # complete. Refuse rather than write a response of unknown coverage.
        if not allow_partial:
            sys.stderr.write(
                f"refusing to answer: {exc}\n"
                "Repair the request file, or pass --allow-partial to write anyway.\n"
            )
            raise typer.Exit(code=3) from exc
        return

    supplied_set = set(supplied)
    unknown = [qid for qid in supplied if qid not in set(asked)]
    if unknown:
        # Not fatal on its own (the coverage check below is what decides),
        # but an id the request never asked is usually a typo for one it
        # did -- which would otherwise leave that question quietly open.
        sys.stderr.write(
            f"warning: answers for ids the request did not ask: {', '.join(unknown)}\n"
            f"         asked: {', '.join(asked) or '(none)'}\n"
        )

    missing = [qid for qid in asked if qid not in supplied_set]
    if missing and not allow_partial:
        sys.stderr.write(
            f"refusing to write a partial response for {task_id} #{sequence:03d}: "
            f"no answer for {', '.join(missing)}\n"
            f"         asked: {', '.join(asked)}\n"
            f"         given: {', '.join(supplied) or '(none)'}\n"
            + (
                f"Pass --merge to carry forward the answers already in {existing_response.name}, "
                "answer every question explicitly, "
                if existing_response is not None
                else "Answer every question, "
            )
            + "or pass --allow-partial to leave the rest open.\n"
        )
        raise typer.Exit(code=3)

    if missing and existing_response is not None:
        # --allow-partial rewrites the whole response file, so say which
        # previously-recorded answers this call is about to discard.
        try:
            previously = answered_question_ids(load_sidecar_payload(existing_response))
        except (QueueIOError, QueueSchemaError):
            return
        dropped = [qid for qid in asked if qid in previously and qid not in supplied_set]
        if dropped:
            sys.stderr.write(
                f"warning: overwriting {existing_response.name} drops existing "
                f"answers for: {', '.join(dropped)}\n"
            )
