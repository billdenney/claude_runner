"""Top-level CLI entry point for ``claude-task-runner``."""

from __future__ import annotations

import os

import typer

from claude_task_runner.cli import (
    account_cmd,
    doctor_cmd,
    install_cmd,
    install_skills_cmd,
    queue_cmd,
    sidecar_cmd,
    supervisor_cmd,
    usage_cmd,
    watchdog_cmd,
)
from claude_task_runner.observability import LogFormat, configure_logging


def _early_configure_logging() -> None:
    """Configure logging before Typer parses args.

    Reads env-var overrides so operator-driven runs (one-off
    ``claude-task-runner doctor --check-api-usage``) can crank to
    DEBUG without editing the queue TOML. The defaults match the
    ``[logging]`` defaults in settings.toml so production behaviour
    is unchanged.

    Settings-driven configuration (per-queue ``[logging]`` block) is
    applied in commands that load settings — see e.g.
    :mod:`cli.supervisor_cmd`. The ``configure_logging`` call is
    idempotent so the later, fully-resolved call is a no-op when the
    env-var path already configured the same.
    """
    level = os.environ.get("CLAUDE_TASK_RUNNER_LOG_LEVEL", "INFO")
    fmt_raw = os.environ.get("CLAUDE_TASK_RUNNER_LOG_FORMAT", "text")
    fmt: LogFormat = fmt_raw if fmt_raw in ("text", "json") else "text"  # type: ignore[assignment]
    configure_logging(level=level, fmt=fmt)


_early_configure_logging()


app = typer.Typer(
    name="claude-task-runner",
    help="Window-aware task runner for Claude Code.",
    no_args_is_help=True,
)
app.add_typer(usage_cmd.app, name="usage", help="Usage capture, parse, and drift check.")
app.add_typer(
    supervisor_cmd.app,
    name="supervisor",
    help="Start, stop, and inspect the supervisor.",
)
app.add_typer(
    account_cmd.app,
    name="account",
    help="List configured accounts; pause/resume per-account dispatch.",
)
app.add_typer(queue_cmd.app, name="queue", help="List and add tasks to a queue.")
app.add_typer(
    sidecar_cmd.app,
    name="sidecar",
    help="List, show, and answer sidecar requests.",
)
app.add_typer(
    install_cmd.app,
    name="install",
    help="Install the watchdog (systemd preferred, cron fallback).",
)
app.add_typer(
    install_skills_cmd.app,
    name="install-skills",
    help="Install the task-runner skills into ~/.claude/skills/.",
)
app.add_typer(
    watchdog_cmd.app,
    name="watchdog",
    help="Watchdog tick (cron / systemd entry-point) and queue registration.",
)
app.add_typer(
    doctor_cmd.app,
    name="doctor",
    help="Self-diagnostic battery (pass/warn/fail per check).",
)


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
