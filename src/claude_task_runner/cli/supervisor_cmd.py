"""``claude-task-runner supervisor start | stop | status`` subcommands.

Thin CLI surface around :mod:`supervisor.daemon` and the persisted
:class:`SupervisorSnapshot` / PID file. ``start`` blocks; ``stop``
asks the supervisor to exit; ``status`` is read-only.
"""

from __future__ import annotations

import json as _json
import os
import signal
from pathlib import Path

import typer
from rich.console import Console

from claude_task_runner.clock import RealClock
from claude_task_runner.config.loader import load_settings
from claude_task_runner.queue.store import (
    list_pending_tasks,
    list_state_files,
    load_state,
    queue_runtime_dir,
)
from claude_task_runner.supervisor import persistence as persist_mod
from claude_task_runner.supervisor import pidfile as pidfile_mod
from claude_task_runner.supervisor.daemon import start_daemon
from claude_task_runner.supervisor.states import SupervisorState
from claude_task_runner.usage.api_source import ApiUsageSource
from claude_task_runner.usage.source import (
    ApiThenTtyUsageSource,
    ClaudeUsageSource,
    UsageSource,
)

app = typer.Typer(no_args_is_help=True)


def _captures_dir(queue_dir: Path) -> Path:
    return queue_dir / ".claude_task_runner" / "usage_captures"


def _count_pending(queue_dir: Path) -> int:
    return sum(1 for _ in list_pending_tasks(queue_dir))


def _count_in_flight(queue_dir: Path) -> int:
    """Count TaskState YAMLs whose status is `running` or `awaiting_sidecar`."""
    n = 0
    for path in list_state_files(queue_dir):
        try:
            state = load_state(path)
        except Exception:
            # Skip malformed state files for the purpose of counting; the
            # ``doctor`` subcommand surfaces them separately.
            continue
        if state.status in ("running", "awaiting_sidecar", "possibly_hung"):
            n += 1
    return n


def _build_tty_source(settings: object, queue_path: Path) -> ClaudeUsageSource:
    return ClaudeUsageSource(
        settings.usage,  # type: ignore[attr-defined]
        RealClock(),
        captures_dir=_captures_dir(queue_path),
        claude_executable=settings.claude.executable,  # type: ignore[attr-defined]
        claude_config_dir=settings.claude.config_dir,  # type: ignore[attr-defined]
    )


def _build_api_source(settings: object) -> ApiUsageSource:
    return ApiUsageSource(
        RealClock(),
        config_dir=settings.claude.config_dir,  # type: ignore[attr-defined]
        probe_model=settings.usage.api_probe_model,  # type: ignore[attr-defined]
        timeout_s=settings.usage.api_timeout_s,  # type: ignore[attr-defined]
    )


def _build_usage_source(settings: object, queue_path: Path) -> UsageSource:
    """Pick a UsageSource based on ``settings.usage.source``.

    Single-account today: the chosen source uses
    ``settings.claude.config_dir`` for both the TTY spawn and the
    OAuth bearer lookup. Multi-account /usage capture (one source per
    account, round-robin scheduled by ``AccountState.last_capture_at``)
    is the next PR's territory; once that lands, this helper grows to
    return a ``MultiAccountUsageSource`` that dispatches per-tick to
    the most-overdue account's per-account TtyUsageSource /
    ApiUsageSource pair.
    """
    mode = settings.usage.source  # type: ignore[attr-defined]
    if mode == "tty":
        return _build_tty_source(settings, queue_path)
    if mode == "api":
        return _build_api_source(settings)
    if mode == "api_then_tty":
        return ApiThenTtyUsageSource(
            api=_build_api_source(settings),
            tty=_build_tty_source(settings, queue_path),
        )
    # Pydantic Literal validation makes this unreachable, but defending
    # against a runtime mutation of the settings object.
    raise ValueError(f"unknown [usage].source: {mode!r}")


@app.command("start")
def start(
    *,
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Per-queue claude_runner.toml."
    ),
    queue_dir: Path = typer.Option(Path.cwd, "--queue", help="Queue directory."),
    max_ticks: int | None = typer.Option(
        None, "--max-ticks", help="Cap loop at N ticks (testing)."
    ),
) -> None:
    """Run the supervisor in the foreground.

    Blocks until SIGTERM/SIGINT or until STOPPED state is reached.
    Acquires the host-wide global lock; raises if another supervisor
    is already running.

    Signals (delivered with ``kill -<NAME> <pid>`` against the PID file
    at ``<queue>/.claude_task_runner/supervisor.pid``):

    * ``SIGTERM`` / ``SIGINT`` — request a clean stop; in-flight
      dispatch threads finish their current attempt (architectural
      invariant 2: in-flight tasks are NOT killed when the supervisor
      exits). Use ``claude-task-runner supervisor stop`` to do this
      from the CLI.
    * ``SIGHUP`` — hot-reload ``claude_runner.toml`` on the next tick
      and rescan ``<queue>/todo/`` for new task YAMLs. In-flight tasks
      keep running with their already-built command-line; the new
      config applies to the NEXT dispatch. Malformed TOML is logged
      and the previous config stays active.
    """
    console = Console()
    settings = load_settings(config)
    queue_path = queue_dir.resolve()
    queue_runtime_dir(queue_path)  # ensure subdirs exist

    source = _build_usage_source(settings, queue_path)

    try:
        handle = start_daemon(
            queue_dir=queue_path,
            settings=settings,
            source=source,
            pending_count_fn=lambda: _count_pending(queue_path),
            in_flight_count_fn=lambda: _count_in_flight(queue_path),
            max_ticks=max_ticks,
            config_path=config,
        )
    except pidfile_mod.SupervisorAlreadyRunning as exc:
        console.print(f"[bold red]supervisor already running:[/] {exc}")
        raise typer.Exit(code=2) from exc

    console.print(
        f"[green]Supervisor exited.[/] State at {handle.state_path}, PID file at {handle.pid_path}"
    )


@app.command("stop")
def stop(
    *,
    queue_dir: Path = typer.Option(Path.cwd, "--queue", help="Queue directory."),
    timeout: float = typer.Option(30.0, "--timeout", help="Seconds to wait for clean exit."),
) -> None:
    """Send SIGTERM to the running supervisor.

    Reads the PID from ``<queue>/.claude_task_runner/supervisor.pid``
    and signals it. Does NOT wait for completion beyond ``timeout``.
    """
    console = Console()
    pid_path = queue_dir.resolve() / ".claude_task_runner" / "supervisor.pid"
    pid = pidfile_mod.read_existing_pid(pid_path)
    if pid is None:
        console.print(f"[yellow]No PID file at {pid_path}[/]")
        raise typer.Exit(code=1)
    if not pidfile_mod.is_pid_alive(pid):
        console.print(f"[yellow]PID {pid} not alive (stale PID file)[/]")
        raise typer.Exit(code=1)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        console.print(f"[yellow]PID {pid} disappeared before SIGTERM[/]")
        raise typer.Exit(code=1) from None
    except PermissionError as exc:
        console.print(f"[bold red]not allowed to signal PID {pid}:[/] {exc}")
        raise typer.Exit(code=2) from exc
    console.print(f"[green]SIGTERM sent to PID {pid}.[/]")


@app.command("status")
def status(
    *,
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Per-queue claude_runner.toml."
    ),
    queue_dir: Path = typer.Option(Path.cwd, "--queue", help="Queue directory."),
    json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Show the supervisor's current state and recent activity."""
    settings = load_settings(config)
    queue_path = queue_dir.resolve()
    state_path = persist_mod.supervisor_state_path(queue_path, settings.supervisor.state_file)
    pid_path = queue_path / ".claude_task_runner" / "supervisor.pid"

    snapshot = persist_mod.load(state_path)
    pid = pidfile_mod.read_existing_pid(pid_path)
    alive = pid is not None and pidfile_mod.is_pid_alive(pid)

    payload: dict[str, object] = {
        "queue_dir": str(queue_path),
        "supervisor_alive": alive,
        "supervisor_pid": pid,
        "snapshot": snapshot.model_dump(mode="json") if snapshot else None,
        "pending": _count_pending(queue_path),
        "in_flight": _count_in_flight(queue_path),
    }

    if json:
        print(_json.dumps(payload, default=str, indent=2))
        return

    console = Console()
    console.print(f"[bold]Queue:[/]            {queue_path}")
    console.print(
        f"[bold]Supervisor PID:[/]   {pid} "
        f"({'[green]alive[/]' if alive else '[yellow]not running[/]'})"
    )
    if snapshot is None:
        console.print("[dim]No supervisor.json — never started here.[/]")
    else:
        state_color = (
            "green"
            if snapshot.state in (SupervisorState.IDLE, SupervisorState.DISPATCHING)
            else (
                "yellow"
                if snapshot.state
                in (SupervisorState.SLOWING_DOWN, SupervisorState.END_OF_WEEK_PUSH)
                else "red"
            )
        )
        console.print(
            f"[bold]State:[/]            "
            f"[{state_color}]{snapshot.state.value}[/]  (since {snapshot.since})"
        )
        console.print(
            f"[bold]5h util:[/]          {snapshot.last_5h_util_pct}%"
            f"   [bold]Weekly util:[/] {snapshot.last_weekly_util_pct}%"
        )
        if snapshot.scheduled_wakeup_at is not None:
            console.print(f"[bold]Next wakeup:[/]      {snapshot.scheduled_wakeup_at}")
        if snapshot.last_drift_message:
            console.print(f"[red]Last drift:[/]       {snapshot.last_drift_message}")
    console.print(f"[bold]Pending:[/]          {payload['pending']}")
    console.print(f"[bold]In-flight:[/]        {payload['in_flight']}")
