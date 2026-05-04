"""``claude-task-runner doctor`` — operator triage entry point.

Runs every check in :mod:`doctor.checks`, prints PASS / WARN / FAIL,
and exits non-zero if anything failed. Tools (CI, the runbook) can
parse ``--json`` output programmatically.
"""

from __future__ import annotations

import json as _json
from pathlib import Path

import typer
from rich.console import Console

from claude_task_runner.config.loader import load_settings
from claude_task_runner.doctor.checks import CheckStatus, all_checks

app = typer.Typer(no_args_is_help=False, invoke_without_command=False)


_STATUS_COLOR = {
    CheckStatus.PASS: "green",
    CheckStatus.WARN: "yellow",
    CheckStatus.FAIL: "red",
}
_STATUS_LABEL = {
    CheckStatus.PASS: "PASS",
    CheckStatus.WARN: "WARN",
    CheckStatus.FAIL: "FAIL",
}


@app.callback(invoke_without_command=True)
def doctor(
    *,
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Per-queue claude_runner.toml."
    ),
    queue_dir: Path = typer.Option(Path.cwd, "--queue", help="Queue directory."),
    json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Run a battery of self-diagnostic checks against this queue.

    Exits 0 if everything is PASS / WARN, 1 if any FAIL.
    """
    settings = load_settings(config)
    queue_path = queue_dir.resolve()

    results = [factory() for factory in all_checks(settings, queue_path)]

    if json:
        payload = {
            "queue_dir": str(queue_path),
            "results": [
                {
                    "name": r.name,
                    "status": r.status.value,
                    "detail": r.detail,
                    "remediation": r.remediation,
                }
                for r in results
            ],
        }
        print(_json.dumps(payload, indent=2))
    else:
        console = Console()
        console.print(f"[bold]Queue:[/] {queue_path}\n")
        for r in results:
            color = _STATUS_COLOR[r.status]
            label = _STATUS_LABEL[r.status]
            console.print(f"  [{color}][bold]{label}[/][/] [bold]{r.name}[/]: {r.detail}")
            if r.remediation:
                for line in r.remediation.splitlines():
                    console.print(f"      [dim]{line}[/]")

    failures = sum(1 for r in results if r.status is CheckStatus.FAIL)
    if failures:
        raise typer.Exit(code=1)
