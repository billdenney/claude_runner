"""``claude-task-runner worktree`` -- reclaim finished tasks' git worktrees.

* ``worktree reclaim`` -- remove the worktree of every ``completed`` task whose
  branch is already merged into ``<remote>/<parent_branch>`` and whose
  ``git status`` is clean (ADR-0034). A dry run unless given ``--apply``.

The conditions, and the ``[worktree_reclaim]`` settings that shape them, are
documented in :mod:`claude_task_runner.worktree.reclaim`.

Exit codes: 0 when the pass ran cleanly (kept worktrees are not an error);
1 when a fetch, a git probe or a removal failed; 2 when the command could
not run at all (not a queue directory, invalid config, unreadable
``supervisor.json``).
"""

from __future__ import annotations

import json as _json
from pathlib import Path

import typer

from claude_task_runner.cli._helpers import resolve_per_queue_config
from claude_task_runner.config.loader import ConfigError, load_settings
from claude_task_runner.config.schema import Settings
from claude_task_runner.supervisor import persistence as persist_mod
from claude_task_runner.worktree import reclaim as reclaim_mod

app = typer.Typer(no_args_is_help=True, rich_markup_mode=None)

_LABELS = {
    reclaim_mod.Outcome.RECLAIMED: "gone",
    reclaim_mod.Outcome.WOULD_RECLAIM: "would",
    reclaim_mod.Outcome.KEPT: "keep",
    reclaim_mod.Outcome.FAILED: "FAIL",
}


def _fail(message: str, *, json: bool) -> typer.Exit:
    """Report a could-not-run error and return the exit to raise."""
    if json:
        print(_json.dumps({"ok": False, "error": message}))
    else:
        typer.echo(f"error: {message}", err=True)
    return typer.Exit(code=2)


def supervisor_in_flight(queue_dir: Path, settings: Settings) -> set[str]:
    """Task ids the supervisor's last persisted snapshot lists as in flight.

    The dispatcher writes ``completed`` before it runs the post-dispatch hook
    inside the worktree, so a task can read ``completed`` while its dispatch
    thread still uses the directory. The supervisor persists its in-flight
    set every tick; a task listed there is kept. A stale snapshot left by a
    dead supervisor only makes the reclaim more conservative.

    Raises :class:`persist_mod.SupervisorPersistenceError` when the snapshot
    exists but cannot be read -- in-flight state is then unknown.
    """
    path = persist_mod.supervisor_state_path(queue_dir, settings.supervisor.state_file)
    snapshot = persist_mod.load(path)
    if snapshot is None:
        return set()
    return set(snapshot.in_flight_task_ids) | {rec.task_id for rec in snapshot.in_flight}


def _describe(result: reclaim_mod.ReclaimResult) -> str:
    notes: list[str] = []
    if result.reason is not None:
        notes.append(f"{result.reason.value}: {result.detail}")
    elif result.detail:
        notes.append(result.detail)
    if result.forced:
        notes.append(
            f"--force discards {len(result.discarded)} untracked path(s): "
            + ", ".join(result.discarded)
        )
    return "; ".join(notes)


@app.command("reclaim")
def reclaim(
    *,
    queue_dir: Path = typer.Option(Path.cwd, "--queue", help="Queue directory."),
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Per-queue claude_runner.toml."
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Remove the worktrees. Without it the command is a dry run "
        "(it still fetches <remote>/<parent_branch>).",
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        min=1,
        help="Stop after this many removals; the rest wait for the next run.",
    ),
    json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Reclaim the git worktrees of completed, merged, clean tasks (ADR-0034).

    A worktree is removed only when ALL of these hold: the task's state says
    ``completed`` and the supervisor does not list it in flight; its branch
    (the worktree_reclaim table's ``branch_template``) is checked out there and
    is an ancestor of ``<remote>/<parent_branch>`` after a fetch; and ``git
    status --porcelain`` is empty apart from untracked ``discardable_untracked``
    paths, which ``--force`` then discards. The local branch is deleted with
    ``git branch -d`` only. Every other worktree is listed with the reason it
    was kept.
    """
    try:
        qd = reclaim_mod.require_queue_dir(queue_dir)
        settings = load_settings(resolve_per_queue_config(config, qd))
    except (reclaim_mod.ReclaimError, ConfigError) as exc:
        raise _fail(str(exc), json=json) from exc
    try:
        in_flight = supervisor_in_flight(qd, settings)
    except persist_mod.SupervisorPersistenceError as exc:
        raise _fail(
            f"cannot tell which tasks are in flight: {exc}; repair or remove the file",
            json=json,
        ) from exc

    try:
        report = reclaim_mod.reclaim_worktrees(
            qd,
            settings.worktree_reclaim,
            apply=apply,
            in_flight_task_ids=in_flight,
            limit=limit,
        )
    except reclaim_mod.ReclaimError as exc:
        raise _fail(str(exc), json=json) from exc

    if json:
        print(_json.dumps(report.to_json(), indent=2))
    else:
        for result in report.results:
            line = f"{_LABELS[result.outcome]:<6} {result.task_id:<45} {_describe(result)}"
            typer.echo(line.rstrip())
        for error in report.errors:
            typer.echo(f"error: {error}", err=True)
        if report.unparseable_tasks:
            typer.echo(
                f"note: {len(report.unparseable_tasks)} task YAML(s) in todo/ could not be "
                "parsed; their worktrees were not considered",
                err=True,
            )
        if report.results:
            typer.echo("")
        typer.echo(report.summary())
    if not report.ok:
        raise typer.Exit(code=1)
