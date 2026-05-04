"""``claude-task-runner watchdog tick`` — internal entry-point for the
cron line / systemd timer.

One tick:

1. Load watchdog settings.
2. Load watchdog state (recent restarts, backoff alerts).
3. For each registered queue (``~/.claude_task_runner/queues.json``):
   read the PID file; ask :func:`cron.backoff.decide` whether to act.
4. On RESTART verdict: spawn ``claude-task-runner supervisor start``
   detached.

Output is structured logs to stdout (the cron wrapper redirects to
``~/.claude_task_runner/watchdog.log``).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import typer

from claude_task_runner.clock import RealClock
from claude_task_runner.config.loader import load_settings
from claude_task_runner.cron import backoff as backoff_mod
from claude_task_runner.supervisor import pidfile as pidfile_mod

QUEUES_REGISTRY_FILENAME = "queues.json"
"""Per-host registry of queue directories the watchdog should manage.

Format: ``{"queues": ["/path/to/queue1", "/path/to/queue2"]}``. The
``install`` subcommand auto-adds the queue directory it was invoked
with."""

app = typer.Typer(no_args_is_help=True)


def queues_registry_path() -> Path:
    return Path.home() / ".claude_task_runner" / QUEUES_REGISTRY_FILENAME


def load_registered_queues() -> list[Path]:
    path = queues_registry_path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    raw = payload.get("queues", [])
    if not isinstance(raw, list):
        return []
    return [Path(q) for q in raw if isinstance(q, str)]


def register_queue(queue_dir: Path) -> None:
    """Add ``queue_dir`` to the registry. Idempotent."""
    path = queues_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = load_registered_queues()
    resolved = queue_dir.resolve()
    if resolved in existing:
        return
    existing.append(resolved)
    path.write_text(json.dumps({"queues": [str(q) for q in existing]}, indent=2) + "\n")


def _supervisor_is_alive(queue_dir: Path) -> tuple[bool, int | None]:
    pid_path = queue_dir / ".claude_task_runner" / "supervisor.pid"
    pid = pidfile_mod.read_existing_pid(pid_path)
    if pid is None:
        return False, None
    return pidfile_mod.is_pid_alive(pid), pid


def _spawn_supervisor(queue_dir: Path) -> int:
    """Start the supervisor detached. Returns the new PID."""
    exe = shutil.which("claude-task-runner")
    if exe is None:
        raise RuntimeError("claude-task-runner not on PATH")
    log_dir = queue_dir / ".claude_task_runner"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "supervisor.log"
    log_fh = open(log_path, "ab")  # noqa: SIM115 — handed to subprocess
    proc = subprocess.Popen(
        [exe, "supervisor", "start", "--queue", str(queue_dir)],
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

    queues = load_registered_queues()
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
                new_pid = _spawn_supervisor(queue_dir)
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
    """Register a queue with the watchdog so future ticks manage it.

    Called automatically by ``install``; expose explicitly so operators
    can add queues without re-running install.
    """
    register_queue(queue_dir)
    print(f"registered: {queue_dir.resolve()}")


@app.command("queues")
def list_queues() -> None:
    """Print the registered queues, one per line."""
    for q in load_registered_queues():
        print(q)
