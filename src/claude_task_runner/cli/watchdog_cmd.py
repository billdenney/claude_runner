"""``claude-task-runner watchdog tick`` — internal entry-point for the
crontab line that a cron ``install`` adds. The systemd install runs no
tick: systemd restarts its unit itself.

One tick:

1. Load watchdog settings.
2. Load watchdog state (recent restarts, backoff alerts).
3. For each registered queue (``~/.claude_task_runner/queues.json``,
   written by a cron ``install`` and by ``watchdog register``; see
   :mod:`cron.registry`):
   skip it with an ERROR line if it is not an existing directory (it was
   deleted or moved after it was registered); otherwise read the PID
   file and ask :func:`cron.backoff.decide` whether to act.
4. On RESTART verdict: spawn ``claude-task-runner supervisor start``
   detached.

Output is structured logs to stdout (the cron wrapper redirects to
``~/.claude_task_runner/watchdog.log``).

Three more subcommands manage the registry that a tick walks:

* ``watchdog register``   — add a queue (``--queue``, default the current
  directory).
* ``watchdog unregister`` — remove a queue (``--queue``, default the
  current directory). The directory need not exist.
* ``watchdog queues``     — print the registered queues, one per line,
  and warn on stderr about each one that is not an existing directory.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import typer

from claude_task_runner.cli._helpers import CWD_DEFAULT_LABEL
from claude_task_runner.clock import RealClock
from claude_task_runner.config.loader import load_settings
from claude_task_runner.cron import backoff as backoff_mod
from claude_task_runner.cron import registry as registry_mod
from claude_task_runner.supervisor import pidfile as pidfile_mod

app = typer.Typer(no_args_is_help=True, rich_markup_mode=None)


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
    # No parents=True: a queue deleted after the tick checked it must stay
    # deleted, not come back as an empty queue whose supervisor would take
    # the per-user global lock.
    log_dir.mkdir(exist_ok=True)
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
        ts = clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")
        # Checked before decide(): every queue shares one restart history,
        # so a skipped queue must not take a restart from the others.
        # os.path.isdir is False where Path.is_dir raises (a parent that
        # denies access), so one such path cannot end the tick early.
        if not os.path.isdir(queue_dir):
            sys.stdout.write(
                f"{ts} watchdog: ERROR queue={queue_dir} is not an existing directory, "
                "so its supervisor was not restarted and the directory was not created. "
                "If the queue moved, register its new path. If it is gone for good, run: "
                f"claude-task-runner watchdog unregister --queue {queue_dir}\n"
            )
            continue

        alive, pid = _supervisor_is_alive(queue_dir)
        decision = backoff_mod.decide(
            state=new_state,
            supervisor_alive=alive,
            settings=settings.watchdog,
            clock=clock,
        )
        new_state = decision.new_state

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
    queue_dir: Path = typer.Option(
        Path.cwd, "--queue", help="Queue directory to register.", show_default=CWD_DEFAULT_LABEL
    ),
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


@app.command("unregister")
def unregister(
    *,
    queue_dir: Path = typer.Option(
        Path.cwd, "--queue", help="Queue directory to unregister.", show_default=CWD_DEFAULT_LABEL
    ),
) -> None:
    """Stop the cron watchdog from managing a queue. Idempotent.

    The directory need not exist, so a queue that was deleted or moved
    can be dropped; a tick skips such a queue but keeps it registered.
    A queue that is not registered is reported and left alone (exit 0).
    A corrupt registry is an error (exit 2) and is left as it was.
    ``install uninstall`` does not change the registry.
    """
    try:
        removed = registry_mod.unregister_queue(queue_dir)
    except (registry_mod.RegistryError, OSError) as exc:
        print(f"unregister failed: {exc}", file=sys.stderr)
        raise typer.Exit(code=2) from exc
    for q in removed:
        print(f"unregistered: {q}")
    if not removed:
        print(f"not registered: {queue_dir.resolve()}")


@app.command("queues")
def list_queues() -> None:
    """Print the registered queues, one per line.

    Warns on stderr about each one that is not an existing directory,
    which a tick skips. Stdout stays one path per line.
    """
    for q in registry_mod.load_registered_queues():
        print(q)
        if not os.path.isdir(q):
            print(
                f"warning: {q} is not an existing directory, so the watchdog skips it. "
                f"To stop managing it, run: claude-task-runner watchdog unregister --queue {q}",
                file=sys.stderr,
            )
