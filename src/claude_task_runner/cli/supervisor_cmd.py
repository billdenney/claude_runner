"""``claude-task-runner supervisor start | stop | status`` subcommands.

Thin CLI surface around :mod:`supervisor.daemon` and the persisted
:class:`SupervisorSnapshot` / PID file. ``start`` blocks; ``stop``
asks the supervisor to exit; ``status`` is read-only.
"""

from __future__ import annotations

import json as _json
import os
import signal
from collections.abc import Callable
from pathlib import Path

import typer
from rich.console import Console

from claude_task_runner.clock import RealClock
from claude_task_runner.config.loader import load_settings
from claude_task_runner.observability import configure_logging
from claude_task_runner.queue.store import (
    list_pending_tasks,
    list_state_files,
    load_state,
    queue_runtime_dir,
)
from claude_task_runner.supervisor import persistence as persist_mod
from claude_task_runner.supervisor import pidfile as pidfile_mod
from claude_task_runner.supervisor.daemon import start_daemon
from claude_task_runner.supervisor.states import SupervisorSnapshot, SupervisorState
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


def _build_tty_source(
    settings: object,
    queue_path: Path,
    *,
    config_dir: str | None = None,
) -> ClaudeUsageSource:
    """Build a TTY usage source against ``config_dir``.

    Defaults to ``settings.claude.config_dir`` (legacy single-account
    path) when ``config_dir`` is None. Multi-account callers pass each
    account's ``config_dir`` explicitly.
    """
    effective = (
        config_dir if config_dir is not None else settings.claude.config_dir  # type: ignore[attr-defined]
    )
    return ClaudeUsageSource(
        settings.usage,  # type: ignore[attr-defined]
        RealClock(),
        captures_dir=_captures_dir(queue_path),
        claude_executable=settings.claude.executable,  # type: ignore[attr-defined]
        claude_config_dir=effective,
    )


def _build_api_source(
    settings: object,
    *,
    config_dir: str | None = None,
) -> ApiUsageSource:
    """Build an API usage source against ``config_dir``.

    Same default rule as :func:`_build_tty_source`.
    """
    effective = (
        config_dir if config_dir is not None else settings.claude.config_dir  # type: ignore[attr-defined]
    )
    return ApiUsageSource(
        RealClock(),
        config_dir=effective,
        probe_model=settings.usage.api_probe_model,  # type: ignore[attr-defined]
        timeout_s=settings.usage.api_timeout_s,  # type: ignore[attr-defined]
    )


def _build_per_account_source(
    settings: object,
    queue_path: Path,
    config_dir: str,
) -> UsageSource:
    """Build one inner source for one account, honouring ``[usage].source``.

    Same mode dispatch as :func:`_build_usage_source` but pinned to
    a specific ``config_dir`` so the multi-account wrapper can map
    one source per configured account.

    PR 14 long-lived token override: when ``<config_dir>/oauth-token``
    exists the account is on a ``claude setup-token`` long-lived
    bearer; the TTY fall-through in ``api_then_tty`` cannot recover
    a revoked long-lived token (the CLI uses the same bearer and will
    also 401), so the right behaviour is to drop the composite and
    use the API source alone. A 401 then surfaces as
    :class:`UsageApiAuthExpired` → ``ERROR_DRIFT`` rather than being
    swallowed by a TTY timeout the supervisor can't act on.
    """
    mode = settings.usage.source  # type: ignore[attr-defined]
    # Local import to avoid pulling oauth_token_file into modules that
    # don't need it (keeps the CLI startup graph slim).
    from claude_task_runner.usage.oauth_token_file import oauth_token_path

    long_lived = oauth_token_path(config_dir).exists()

    if mode == "tty":
        # Long-lived bearer + tty-only source: still build the TTY
        # source; the CLI will pick up CLAUDE_CODE_OAUTH_TOKEN at
        # spawn time (PR 14 dispatcher change). No composite to undo.
        return _build_tty_source(settings, queue_path, config_dir=config_dir)
    if mode == "api":
        return _build_api_source(settings, config_dir=config_dir)
    if mode == "api_then_tty":
        if long_lived:
            return _build_api_source(settings, config_dir=config_dir)
        return ApiThenTtyUsageSource(
            api=_build_api_source(settings, config_dir=config_dir),
            tty=_build_tty_source(settings, queue_path, config_dir=config_dir),
        )
    raise ValueError(f"unknown [usage].source: {mode!r}")


def _build_usage_source(
    settings: object,
    queue_path: Path,
    snapshot_getter: Callable[[], object],
) -> UsageSource:
    """Pick a UsageSource based on ``settings.usage.source`` and account count.

    Single-account (``len(settings.accounts) <= 1``): direct
    ClaudeUsageSource / ApiUsageSource / composite, same as PR 6.

    Multi-account (``len(settings.accounts) > 1``): wrap one
    per-account source per ``[[accounts]]`` block in a
    :class:`MultiAccountUsageSource` that round-robins captures by
    ``AccountState.last_capture_at``. The reading is tagged with the
    captured account and the daemon attributes it to
    ``snapshot.accounts[<name>]``.

    ``snapshot_getter`` is a zero-arg callable that returns the
    current :class:`SupervisorSnapshot`. The multi-account wrapper
    needs the FRESHEST snapshot per call to consult the
    ``last_capture_at`` fields the daemon just persisted.
    """
    accounts = settings.accounts  # type: ignore[attr-defined]
    if len(accounts) <= 1:
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
        raise ValueError(f"unknown [usage].source: {mode!r}")

    # Multi-account: one inner source per account.
    per_account: dict[str, UsageSource] = {
        acct.name: _build_per_account_source(settings, queue_path, acct.config_dir)
        for acct in accounts
    }
    # Local import to keep the CLI module's import graph slim.
    from claude_task_runner.usage.multi_account_source import MultiAccountUsageSource

    return MultiAccountUsageSource(
        per_account_sources=per_account,
        snapshot_getter=snapshot_getter,  # type: ignore[arg-type]
    )


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
    # Re-apply logging settings now that the queue's [logging] block has
    # been read. The CLI entry point's early configure runs before Typer
    # parses arguments (so it can use env-var overrides) with safe
    # defaults; this call upgrades to the operator-configured level /
    # format. No-op when the settings happen to match the env-var
    # defaults.
    configure_logging(level=settings.logging.level, fmt=settings.logging.format)

    queue_path = queue_dir.resolve()
    queue_runtime_dir(queue_path)  # ensure subdirs exist

    # Use source_builder so the multi-account wrapper can be wired to
    # the daemon's live snapshot accessor — the round-robin picker
    # needs the freshest accounts[*].last_capture_at every read.
    def _source_builder(snapshot_getter: Callable[[], SupervisorSnapshot]) -> UsageSource:
        return _build_usage_source(settings, queue_path, snapshot_getter)

    try:
        handle = start_daemon(
            queue_dir=queue_path,
            settings=settings,
            source_builder=_source_builder,
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


@app.command("drain")
def drain(
    *,
    queue_dir: Path = typer.Option(Path.cwd, "--queue", help="Queue directory."),
    wait: bool = typer.Option(
        True,
        "--wait/--no-wait",
        help="Block until the supervisor exits (or --timeout elapses).",
    ),
    timeout: float = typer.Option(
        3600.0,
        "--timeout",
        help=(
            "When --wait, give up after N seconds. Default 1h — longer "
            "than the longest plausible task. Exit code 4 if the timeout "
            "fires; the supervisor will keep draining."
        ),
    ),
    poll_s: float = typer.Option(
        2.0,
        "--poll",
        help="When --wait, seconds between PID-liveness checks.",
    ),
) -> None:
    """Graceful drain: stop dispatching NEW work; exit when in_flight=0.

    Sends SIGUSR1 to the running supervisor. The supervisor stops
    picking up new tasks immediately but keeps ticking so its reaper
    sees in-flight completions; once every dispatched thread has
    finished, the supervisor exits cleanly. The persisted snapshot
    contains terminal state for every task that ran on it — a fresh
    supervisor started afterwards re-reads ``supervisor.json`` and
    picks up the queue without double-dispatching anything.

    Combined with the systemd unit's ``ExecStop=... drain`` directive
    and ``Restart=on-success``, this gives near-zero-downtime
    supervisor restarts with zero lost work. The drain window is
    bounded by the longest in-flight task (typically minutes for
    extraction work; up to ``[task_caps].max_duration_s_per_task``
    for the hard cap).

    Exit codes:
      0  supervisor exited cleanly (or --no-wait and signal delivered)
      1  no PID file / stale PID file
      2  signal delivery rejected (permission)
      4  --wait timed out (supervisor still draining — re-run drain or stop)
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
        os.kill(pid, signal.SIGUSR1)
    except ProcessLookupError:
        console.print(f"[yellow]PID {pid} disappeared before SIGUSR1[/]")
        raise typer.Exit(code=1) from None
    except PermissionError as exc:
        console.print(f"[bold red]not allowed to signal PID {pid}:[/] {exc}")
        raise typer.Exit(code=2) from exc
    console.print(f"[green]SIGUSR1 (drain) sent to PID {pid}.[/]")

    if not wait:
        return

    import time

    console.print(
        f"[dim]Waiting up to {timeout:.0f}s for PID {pid} to exit "
        f"(polling every {poll_s:.0f}s)...[/]"
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pidfile_mod.is_pid_alive(pid):
            console.print(f"[green]PID {pid} exited; drain complete.[/]")
            return
        time.sleep(poll_s)
    console.print(
        f"[bold yellow]Drain still in progress after {timeout:.0f}s.[/] "
        "The supervisor will keep draining. Re-run `supervisor drain` "
        "to wait further, or `supervisor stop` to force-exit (in-flight "
        "tasks will be killed by systemd's KillMode)."
    )
    raise typer.Exit(code=4)


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
