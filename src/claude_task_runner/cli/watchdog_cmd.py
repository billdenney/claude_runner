"""``claude-task-runner watchdog tick`` — internal entry-point for the
crontab line that a cron ``install`` adds. The systemd install runs no
tick: systemd restarts its unit itself.

One tick:

1. Load watchdog settings.
2. Load watchdog state (recent restarts, backoff alerts).
3. For each registered queue (``~/.claude_task_runner/queues.json``,
   written by a cron ``install`` and by ``watchdog register``; see
   :mod:`cron.registry`):
   read the PID file; ask :func:`cron.backoff.decide` whether to act.
4. On RESTART verdict: spawn ``claude-task-runner supervisor start``
   detached.

Output is structured logs to stdout (the cron wrapper redirects to
``~/.claude_task_runner/watchdog.log``).

Two more subcommands manage the registry that a tick walks:

* ``watchdog register`` — add a queue (``--queue``, default the current
  directory).
* ``watchdog queues``   — print the registered queues, one per line.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import typer

from claude_task_runner.clock import RealClock
from claude_task_runner.config.loader import load_settings
from claude_task_runner.cron import backoff as backoff_mod
from claude_task_runner.cron import registry as registry_mod
from claude_task_runner.supervisor import pidfile as pidfile_mod

app = typer.Typer(no_args_is_help=True)


def _supervisor_is_alive(queue_dir: Path) -> tuple[bool, int | None]:
    pid_path = queue_dir / ".claude_task_runner" / "supervisor.pid"
    pid = pidfile_mod.read_existing_pid(pid_path)
    if pid is None:
        return False, None
    return pidfile_mod.is_pid_alive(pid), pid


def _spawn_supervisor(queue_dir: Path, config: Path | None = None) -> int:
    """Start the supervisor detached. Returns the new PID.

    Forwards ``--config`` so the spawned supervisor loads the SAME
    settings the watchdog used to make its restart decision. Without
    this, the supervisor falls back to package defaults and its
    throttle / backoff policy silently diverges from the operator's
    ``claude_runner.toml``."""
    exe = shutil.which("claude-task-runner")
    if exe is None:
        raise RuntimeError("claude-task-runner not on PATH")
    log_dir = queue_dir / ".claude_task_runner"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "supervisor.log"
    log_fh = open(log_path, "ab")  # noqa: SIM115 — handed to subprocess
    cmd = [exe, "supervisor", "start", "--queue", str(queue_dir)]
    if config is not None:
        cmd += ["--config", str(config)]
    proc = subprocess.Popen(
        cmd,
        stdout=log_fh,
        stderr=log_fh,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    return proc.pid


@app.command("tick")
def tick(
    *,
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Per-queue claude_runner.toml."
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Decide but don't spawn anything."),
) -> None:
    """One watchdog tick: examine each registered queue and act."""
    settings = load_settings(config)
    clock = RealClock()
    state_path = backoff_mod.watchdog_state_path()

    try:
        state = backoff_mod.load_state(state_path)
    except backoff_mod.WatchdogStateError as exc:
        # Don't crash the watchdog on a bad state file — log and reset.
        sys.stdout.write(f"watchdog: bad state file ({exc}); resetting\n")
        state = backoff_mod.WatchdogState()

    queues = registry_mod.load_registered_queues()
    if not queues:
        sys.stdout.write("watchdog: no queues registered; nothing to do\n")
        return

    new_state = state
    for queue_dir in queues:
        alive, pid = _supervisor_is_alive(queue_dir)
        decision = backoff_mod.decide(
            state=new_state,
            supervisor_alive=alive,
            settings=settings.watchdog,
            clock=clock,
        )
        new_state = decision.new_state

        ts = clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")
        sys.stdout.write(
            f"{ts} watchdog queue={queue_dir} alive={alive} pid={pid} "
            f"verdict={decision.verdict.value} detail={decision.detail!r}\n"
        )

        if decision.verdict is backoff_mod.WatchdogVerdict.RESTART and not dry_run:
            try:
                new_pid = _spawn_supervisor(queue_dir, config)
            except Exception as exc:
                sys.stdout.write(f"{ts} watchdog: spawn failed for {queue_dir}: {exc}\n")
            else:
                sys.stdout.write(
                    f"{ts} watchdog: spawned supervisor for {queue_dir} as pid={new_pid}\n"
                )

    backoff_mod.write_state_atomic(new_state, state_path)


@app.command("register")
def register(
    *,
    queue_dir: Path = typer.Option(Path.cwd, "--queue", help="Queue directory to register."),
) -> None:
    """Register a queue with the cron watchdog so its ticks manage it.

    A cron ``install`` registers its ``--queue`` itself; this registers
    one without re-running ``install``. The systemd unit does not read
    this registry. ``watchdog queues`` lists what is registered.
    """
    try:
        registry_mod.register_queue(queue_dir)
    except OSError as exc:
        print(f"register failed: {exc}", file=sys.stderr)
        raise typer.Exit(code=2) from exc
    print(f"registered: {queue_dir.resolve()}")


@app.command("queues")
def list_queues() -> None:
    """Print the registered queues, one per line."""
    for q in registry_mod.load_registered_queues():
        print(q)
