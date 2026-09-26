"""``claude-task-runner supervisor start | stop | drain | status`` subcommands.

Thin CLI surface around :mod:`supervisor.daemon` and the persisted
:class:`SupervisorSnapshot` / PID file. ``start`` blocks; ``stop``
asks the supervisor to exit; ``drain`` stops new dispatches and lets
the supervisor exit once its in-flight tasks finish; ``status`` is
read-only.
"""

from __future__ import annotations

import json as _json
import logging
import math
import os
import signal
import time
from collections.abc import Callable
from pathlib import Path

import typer
from rich.console import Console

from claude_task_runner.cli._helpers import (
    CWD_DEFAULT_LABEL,
    require_queue_option,
    resolve_per_queue_config,
)
from claude_task_runner.clock import RealClock
from claude_task_runner.config.loader import ConfigError, load_settings
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

app = typer.Typer(no_args_is_help=True, rich_markup_mode=None)

logger = logging.getLogger(__name__)


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
        except Exception as exc:
            # Skip malformed state files for the purpose of counting; the
            # ``doctor`` subcommand surfaces them separately. Still warn
            # so a silently-unparseable state file leaves a trace.
            logger.warning("skipping unparseable state file %s: %s", path, exc)
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
    queue_dir: Path = typer.Option(
        Path.cwd, "--queue", help="Queue directory.", show_default=CWD_DEFAULT_LABEL
    ),
    max_ticks: int | None = typer.Option(
        None, "--max-ticks", help="Cap loop at N ticks (testing)."
    ),
) -> None:
    """Run the supervisor in the foreground.

    Blocks until SIGTERM/SIGINT, or until a drain finishes.
    Acquires the host-wide global lock; raises if another supervisor
    is already running.

    Signals (delivered with ``kill -<NAME> <pid>`` against the PID file
    at ``<queue>/.claude_task_runner/supervisor.pid``):

    \b
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
    queue_path = require_queue_option(queue_dir, console)
    settings = load_settings(resolve_per_queue_config(config, queue_path))
    # Re-apply logging settings now that the queue's [logging] block has
    # been read. The CLI entry point's early configure runs before Typer
    # parses arguments (so it can use env-var overrides) with safe
    # defaults; this call upgrades to the operator-configured level /
    # format. No-op when the settings happen to match the env-var
    # defaults.
    configure_logging(level=settings.logging.level, fmt=settings.logging.format)

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


def _say(console: Console, message: str, style: str) -> None:
    """Print ``message`` in ``style``, as written and on one line.

    Rich markup would drop a ``[word]`` from a queue path or from a config
    table name such as ``[supervisor]``, and a ``[/]`` in a path raised
    ``MarkupError``. Printed as
    :func:`~claude_task_runner.cli._helpers.require_queue_option` prints.
    """
    console.print(message, style=style, markup=False, highlight=False, soft_wrap=True)


def _pid_to_signal(queue_path: Path, console: Console) -> int:
    """Return the PID of the supervisor running for ``queue_path``, or exit 1.

    Exits 1 when there is no PID file, when the file holds no PID, and when
    the PID is not alive. A file that holds no PID used to read as no file,
    but a supervisor may still be running then.
    """
    pid_path = queue_path / ".claude_task_runner" / "supervisor.pid"
    try:
        pid = pidfile_mod.read_pid_file(pid_path)
    except pidfile_mod.PidFileUnreadable as exc:
        _say(
            console,
            f"{exc}, so nothing was signalled. A supervisor may still be running: "
            "`pgrep -af 'supervisor start'` lists them.",
            "yellow",
        )
        raise typer.Exit(code=1) from exc
    if pid is None:
        _say(console, f"No PID file at {pid_path}", "yellow")
        raise typer.Exit(code=1)
    if not pidfile_mod.is_pid_alive(pid):
        _say(console, f"PID {pid} not alive (stale PID file)", "yellow")
        raise typer.Exit(code=1)
    return pid


def _send_signal(pid: int, signum: signal.Signals, console: Console) -> None:
    """Send ``signum`` to ``pid``. Exits 1 if it is gone, 2 if not allowed."""
    try:
        os.kill(pid, signum)
    except ProcessLookupError as exc:
        _say(console, f"PID {pid} disappeared before {signum.name}", "yellow")
        raise typer.Exit(code=1) from exc
    except PermissionError as exc:
        _say(console, f"not allowed to signal PID {pid}: {exc}", "bold red")
        raise typer.Exit(code=2) from exc


def _seconds(value: float) -> str:
    """``value`` for a message: ``3600`` for 3600.0, ``0.5`` for 0.5.

    ``{:.0f}`` printed ``--poll 0.5`` as "polling every 0s"."""
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _default_drain_wait(config: Path | None, queue_path: Path, console: Console) -> float | None:
    """How long a waiting drain waits without ``--timeout``; ``None`` is no limit.

    An attempt that starts just before the drain runs its pre-dispatch
    hook, then up to ``[task_caps].max_duration_s_per_task``, then its
    post-dispatch hook, and the supervisor sees its dispatch thread has
    finished on its next tick, up to ``[usage].poll_interval_s`` later.
    The default is the sum. The systemd unit's ``TimeoutStopSec`` is the
    cap alone, because at that timeout systemd kills the supervisor; here
    the timeout only ends the wait, so the margin costs nothing when the
    drain ends sooner. A cap of 0 is no limit, so the wait has none either.

    Reads the queue's settings as ``supervisor start`` does: ``--config``,
    or ``<queue>/claude_runner.toml`` when it exists. When they do not
    load, exits 2 before anything is signalled.
    """
    try:
        settings = load_settings(resolve_per_queue_config(config, queue_path))
    except (ConfigError, OSError) as exc:
        _say(
            console,
            f"cannot load the settings for the default --timeout: {exc}. "
            "Pass --timeout <seconds>, or --no-wait, to drain without them.",
            "bold red",
        )
        raise typer.Exit(code=2) from exc
    cap = settings.task_caps.max_duration_s_per_task
    if cap == 0:
        return None
    return (
        settings.hooks.pre_dispatch_timeout_s
        + cap
        + settings.hooks.post_dispatch_timeout_s
        + settings.usage.poll_interval_s
    )


def _wait_for_exit(pid: int, wait_s: float | None, poll_s: float, console: Console) -> None:
    """Check ``pid`` every ``poll_s`` seconds until it exits; exit 4 after ``wait_s``.

    ``None`` waits without limit.
    """
    limit_s = math.inf if wait_s is None else wait_s
    how_long = "with no time limit" if math.isinf(limit_s) else f"up to {_seconds(limit_s)}s"
    _say(
        console,
        f"Waiting {how_long} for PID {pid} to exit (polling every {_seconds(poll_s)}s)...",
        "dim",
    )
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        if not pidfile_mod.is_pid_alive(pid):
            _say(console, f"PID {pid} exited; drain complete.", "green")
            return
        time.sleep(poll_s)
    _say(
        console,
        f"Drain still in progress after {_seconds(limit_s)}s. The supervisor keeps "
        "draining; re-run `supervisor drain` to wait again. `supervisor stop` would "
        "not end the tasks it is waiting for: with [supervisor].adopt_workers on, the "
        "next supervisor adopts them, and with it off, the supervisor waits for them "
        "before it exits.",
        "bold yellow",
    )
    raise typer.Exit(code=4)


@app.command("stop")
def stop(
    *,
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help=(
            "Per-queue claude_runner.toml. Accepted for symmetry with "
            "`supervisor start` so the systemd unit's fast-stop "
            "`ExecStop=... supervisor stop ...` line can reuse the same "
            "argv as `ExecStart` (ADR-0025). Stop itself only signals the "
            "running supervisor via the queue's pidfile, so this is a "
            "no-op — accepted to avoid `No such option: --config` when "
            "the unit's ExecStop runs."
        ),
    ),
    queue_dir: Path = typer.Option(
        Path.cwd, "--queue", help="Queue directory.", show_default=CWD_DEFAULT_LABEL
    ),
) -> None:
    """Send SIGTERM to the running supervisor, and return.

    Reads the PID from ``<queue>/.claude_task_runner/supervisor.pid``
    and sends it one SIGTERM. Stop does not wait for the supervisor to
    exit; ``supervisor status`` shows when it has.

    When ``[supervisor].adopt_workers`` is on (ADR-0025) this is the
    fast-stop path: the SIGTERM trips the daemon's fast-stop handler, so
    the supervisor stops dispatching and exits without waiting for its
    workers, which keep running, file-backed, for the next supervisor to
    adopt. The systemd unit's ``ExecStop`` is wired here in that mode, so
    stop must return at once. With adoption off, the supervisor stops
    dispatching too, but its process exits only once its in-flight tasks
    finish.

    \b
    Exit codes:
      0  SIGTERM sent
      1  no supervisor to signal: no PID file, one that holds no PID,
         or a PID that is not alive
      2  --queue is not an existing directory, or signal delivery
         rejected (permission)
    """
    _ = config  # accepted for ExecStop symmetry; stop needs no settings.
    console = Console()
    queue_path = require_queue_option(queue_dir, console)
    pid = _pid_to_signal(queue_path, console)
    _send_signal(pid, signal.SIGTERM, console)
    _say(console, f"SIGTERM sent to PID {pid}.", "green")


@app.command("drain")
def drain(
    *,
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help=(
            "Per-queue claude_runner.toml, for the default --timeout. "
            "Defaults to <queue>/claude_runner.toml when that exists. Only "
            "a waiting drain without --timeout reads it, so the systemd "
            "unit's `ExecStop=... drain ... --no-wait` line, which reuses "
            "the argv of `ExecStart` (see "
            "`cron/systemd_unit.py::_drain_command_from`), never does."
        ),
    ),
    queue_dir: Path = typer.Option(
        Path.cwd, "--queue", help="Queue directory.", show_default=CWD_DEFAULT_LABEL
    ),
    wait: bool = typer.Option(
        True,
        "--wait/--no-wait",
        help="Block until the supervisor exits (or --timeout elapses).",
    ),
    timeout: float | None = typer.Option(
        None,
        "--timeout",
        help=(
            "When --wait, give up after N seconds with exit 4; the "
            "supervisor keeps draining. Default: the queue's "
            "[task_caps].max_duration_s_per_task, plus "
            "[hooks].pre_dispatch_timeout_s, "
            "[hooks].post_dispatch_timeout_s and one "
            "[usage].poll_interval_s; no limit when the cap is 0."
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

    A drained supervisor exits 0, and under systemd it stays down. The
    unit is ``Restart=on-failure`` with ``RestartPreventExitStatus=0``,
    so systemd restarts the supervisor only when it fails, never after
    a clean exit. To restart under systemd without losing work, run
    ``systemctl --user restart claude-task-runner`` instead of
    ``drain``. It runs the unit's ``ExecStop`` and then starts a new
    supervisor. ``ExecStop`` is ``supervisor drain --no-wait`` only
    when ``[supervisor].adopt_workers`` is false. By default it is
    ``supervisor stop``, and the new supervisor adopts the running
    workers (ADR-0025). Without systemd, follow ``drain`` with
    ``supervisor start``, or let the cron watchdog restart it. The
    watchdog manages one queue, the last that ``watchdog queues`` lists.

    A drain lasts as long as its longest in-flight task: the
    pre-dispatch hook, the run, up to
    ``[task_caps].max_duration_s_per_task`` or the task's
    ``max_duration_s_override``, and the post-dispatch hook. Without
    ``--timeout``, a waiting drain waits for a task that starts just
    before it and runs to the queue's cap: the cap, both hook timeouts,
    and one ``[usage].poll_interval_s`` for the supervisor to notice.
    With a cap of 0 it waits without limit. That default is the only
    thing drain reads the queue's settings for, so a
    ``claude_runner.toml`` that does not load stops only a waiting
    drain without ``--timeout``, with exit 2 before anything is
    signalled. Pass ``--timeout`` for a task whose override is longer
    than the cap.

    \b
    Exit codes:
      0  supervisor exited (or --no-wait and signal delivered)
      1  no supervisor to signal: no PID file, one that holds no PID,
         or a PID that is not alive
      2  --queue is not an existing directory, the settings for the
         default --timeout did not load, or signal delivery rejected
         (permission)
      4  --wait timed out (the supervisor is still draining)
    """
    console = Console()
    queue_path = require_queue_option(queue_dir, console)
    pid = _pid_to_signal(queue_path, console)
    wait_s = timeout
    if wait and timeout is None:
        # Before signalling, so a TOML that does not load leaves the
        # supervisor as it was.
        wait_s = _default_drain_wait(config, queue_path, console)
    _send_signal(pid, signal.SIGUSR1, console)
    _say(console, f"SIGUSR1 (drain) sent to PID {pid}.", "green")
    if wait:
        _wait_for_exit(pid, wait_s, poll_s, console)


@app.command("status")
def status(
    *,
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Per-queue claude_runner.toml."
    ),
    queue_dir: Path = typer.Option(
        Path.cwd, "--queue", help="Queue directory.", show_default=CWD_DEFAULT_LABEL
    ),
    json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Show the supervisor's current state and recent activity."""
    console = Console()
    queue_path = require_queue_option(queue_dir, console, json=json)
    settings = load_settings(resolve_per_queue_config(config, queue_path))
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
            else ("yellow" if snapshot.state is SupervisorState.SLOWING_DOWN else "red")
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
