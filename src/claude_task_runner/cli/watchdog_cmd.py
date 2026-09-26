"""``claude-task-runner watchdog tick`` — internal entry-point for the
crontab line that a cron ``install`` adds. The systemd install runs no
tick: systemd restarts its unit itself.

One tick:

1. Find the one queue the watchdog manages: the last in
   ``~/.claude_task_runner/queues.json``, which a cron ``install`` and
   ``watchdog register`` write (see :mod:`cron.registry`). Only one
   supervisor runs per user, so any other queue listed there is ignored,
   with a WARNING line.
2. Load watchdog state: the restarts since the supervisor last stayed
   up (see :mod:`cron.backoff`). It belongs to one queue, so a tick that
   finds another queue registered starts it empty.
3. Skip the queue with an ERROR line if it is not an existing directory
   (it was deleted or moved after it was registered).
4. Load the queue's config: the tick's own ``--config``, else the one
   recorded in the registry by ``install --config`` or ``watchdog
   register --config``, else ``<queue>/claude_runner.toml`` if it
   exists, else the package defaults. If it does not load, log an ERROR
   line, leave the supervisor alone and exit 1 after step 7.
5. Read the queue's PID file; when its supervisor is down, check
   whether another process holds ``global.lock``; and ask
   :func:`cron.backoff.decide`, with the config's ``[watchdog]``,
   whether to act.
6. On RESTART verdict: spawn ``claude-task-runner supervisor start``
   detached, with ``--config`` naming the same file.
7. Save the state, unless ``--dry-run``.

Output is structured logs to stdout (the cron wrapper redirects to
``~/.claude_task_runner/watchdog.log``).

Three more subcommands manage the registry that a tick reads:

* ``watchdog register``   — make a queue the one the watchdog manages
  (``--queue``, default the current directory), replacing the queue
  registered before. ``--config`` records the queue's
  ``claude_runner.toml``.
* ``watchdog unregister`` — remove a queue (``--queue``, default the
  current directory). The directory need not exist.
* ``watchdog queues``     — print the registered queues, one per line,
  and warn on stderr about any that the watchdog ignores, and about the
  managed queue when it is not an existing directory.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import typer

from claude_task_runner.cli._helpers import CWD_DEFAULT_LABEL, resolve_per_queue_config
from claude_task_runner.clock import Clock, RealClock
from claude_task_runner.config.loader import ConfigError, load_settings
from claude_task_runner.config.schema import WatchdogSettings
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
        None,
        "--config",
        "-c",
        help=(
            "claude_runner.toml to use instead of the managed queue's own: the one "
            "recorded by install or register --config, else <queue>/claude_runner.toml."
        ),
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Decide and log, but start nothing and save no state."
    ),
) -> None:
    """One watchdog tick: examine the queue the watchdog manages and act.

    Exits 1 when the queue's config does not load, after logging an
    ERROR line; the supervisor is then neither checked nor restarted.
    """
    clock = RealClock()
    state_path = backoff_mod.watchdog_state_path()

    try:
        state = backoff_mod.load_state(state_path)
    except backoff_mod.WatchdogStateError as exc:
        # Don't crash the watchdog on a bad state file — log and reset.
        sys.stdout.write(f"watchdog: bad state file ({exc}); resetting\n")
        state = backoff_mod.WatchdogState()

    registry = registry_mod.load_registry()
    queues = registry.queues
    queue_dir = registry_mod.managed_queue(queues)
    if queue_dir is None:
        sys.stdout.write("watchdog: no queues registered; nothing to do\n")
        return

    ts = clock.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    ignored = registry_mod.ignored_queues(queues)
    if ignored:
        sys.stdout.write(
            f"{ts} watchdog: WARNING queues.json lists {len(ignored) + 1} queues, but one "
            "supervisor runs per user, so the watchdog manages only the last one, "
            f"{queue_dir}, and ignores {', '.join(str(q) for q in ignored)}. To register "
            "just one, run: claude-task-runner watchdog register --queue <queue>\n"
        )

    if state.queue != queue_dir:
        # Restarts counted for another queue must not hold this one back.
        if state.queue is not None:
            sys.stdout.write(
                f"{ts} watchdog: now managing {queue_dir}, not {state.queue}; "
                "its restart history starts empty\n"
            )
        state = backoff_mod.WatchdogState(queue=queue_dir)

    new_state = state
    config_failed = False
    # os.path.isdir is False where Path.is_dir raises (a parent that
    # denies access), so such a path is reported instead of ending the tick.
    if not os.path.isdir(queue_dir):
        sys.stdout.write(
            f"{ts} watchdog: ERROR queue={queue_dir} is not an existing directory, "
            "so its supervisor was not restarted and the directory was not created. "
            "If the queue moved, register its new path. If it is gone for good, run: "
            f"claude-task-runner watchdog unregister --queue {queue_dir}\n"
        )
    else:
        queue_config = _queue_config(config, registry, queue_dir)
        try:
            settings = load_settings(queue_config)
        except ConfigError as exc:
            # A supervisor started with this config would fail to load it too.
            config_failed = True
            sys.stdout.write(
                f"{ts} watchdog: ERROR queue={queue_dir} config={queue_config} does not "
                f"load, so its supervisor was not checked or restarted: {_one_line(exc)}\n"
            )
        else:
            new_state = _decide_and_act(
                queue_dir=queue_dir,
                queue_config=queue_config,
                watchdog=settings.watchdog,
                state=state,
                clock=clock,
                ts=ts,
                dry_run=dry_run,
            )

    # A dry run starts nothing, so the restart it approved must not count
    # toward the next real tick's cooldown or crash-loop threshold.
    if not dry_run:
        backoff_mod.write_state_atomic(new_state, state_path)
    if config_failed:
        raise typer.Exit(code=1)


def _one_line(exc: Exception) -> str:
    """``exc``'s message on one line, so each watchdog.log entry stays one line.

    A pydantic validation error spans several lines; they are joined
    with ``"; "``, and spaces within a line are kept."""
    return "; ".join(line.strip() for line in str(exc).splitlines() if line.strip())


def _queue_config(
    explicit: Path | None, registry: registry_mod.Registry, queue_dir: Path
) -> Path | None:
    """The ``claude_runner.toml`` a tick uses for ``queue_dir``; ``None`` for the defaults.

    In order: the tick's own ``--config``; the config ``install --config``
    or ``watchdog register --config`` recorded for the queue; then, as
    ``supervisor start`` finds it, ``<queue>/claude_runner.toml`` if it
    exists."""
    if explicit is not None:
        return explicit
    recorded = registry.configs.get(queue_dir)
    if recorded is not None:
        return recorded
    return resolve_per_queue_config(None, queue_dir)


def _decide_and_act(
    *,
    queue_dir: Path,
    queue_config: Path | None,
    watchdog: WatchdogSettings,
    state: backoff_mod.WatchdogState,
    clock: Clock,
    ts: str,
    dry_run: bool,
) -> backoff_mod.WatchdogState:
    """Decide for ``queue_dir`` with its ``[watchdog]``, restart it if approved.

    Returns the state to save. A restart passes ``queue_config`` to the
    supervisor, so it runs with the settings the decision used."""
    alive, pid = _supervisor_is_alive(queue_dir)
    lock_held, lock_pid = False, None
    if not alive:
        # Only when the supervisor is down, so a healthy tick never
        # holds the lock, even for the moment a probe does.
        lock_held, lock_pid = _probe_global_lock(ts)
    decision = backoff_mod.decide(
        state=state,
        supervisor_alive=alive,
        lock_held=lock_held,
        lock_holder_pid=lock_pid,
        settings=watchdog,
        clock=clock,
    )

    sys.stdout.write(
        f"{ts} watchdog queue={queue_dir} alive={alive} pid={pid} "
        f"verdict={decision.verdict.value} detail={decision.detail!r}\n"
    )

    if decision.verdict is backoff_mod.WatchdogVerdict.RESTART and not dry_run:
        try:
            new_pid = _spawn_supervisor(queue_dir, queue_config)
        except Exception as exc:
            sys.stdout.write(f"{ts} watchdog: spawn failed for {queue_dir}: {exc}\n")
        else:
            with_config = f" with --config {queue_config}" if queue_config is not None else ""
            sys.stdout.write(
                f"{ts} watchdog: spawned supervisor for {queue_dir} as pid={new_pid}{with_config}\n"
            )
    return decision.new_state


def _probe_global_lock(ts: str) -> pidfile_mod.GlobalLockProbe:
    """Probe ``global.lock``; if that fails, log it and report the lock free.

    Free is how a tick decided before it probed: it goes ahead with the
    restart, and a supervisor that cannot take the lock says why in its
    own log."""
    try:
        return pidfile_mod.probe_global_lock()
    except OSError as exc:
        sys.stdout.write(
            f"{ts} watchdog: ERROR could not check global.lock ({exc}); "
            "deciding as if no other supervisor held it\n"
        )
        return pidfile_mod.GlobalLockProbe(held=False, pid=None)


@app.command("register")
def register(
    *,
    queue_dir: Path = typer.Option(
        Path.cwd, "--queue", help="Queue directory to register.", show_default=CWD_DEFAULT_LABEL
    ),
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help=(
            "claude_runner.toml for the watchdog to use for this queue, recorded in "
            "queues.json. Without it, a tick uses <queue>/claude_runner.toml if it exists."
        ),
    ),
) -> None:
    """Make a queue the one the cron watchdog manages.

    One supervisor runs per user, so the watchdog manages one queue, as
    the systemd unit runs one. This replaces the queue registered before
    and prints each one it replaced. A cron ``install`` registers its
    ``--queue`` the same way. While the replaced queue's supervisor still
    holds the per-user lock, ticks start none; this says so, and how to
    hand over. The systemd unit does not read this registry.
    ``watchdog queues`` shows what is registered.

    A tick takes the queue's [watchdog] settings from its config and
    starts the supervisor with the same file. That is ``--config``,
    recorded as an absolute path, else <queue>/claude_runner.toml if it
    exists, else the package defaults. Registering again without
    ``--config`` drops a recorded one. A config that does not load is
    refused here, since every tick would fail on it.
    """
    recorded = config.absolute() if config is not None else None
    try:
        load_settings(resolve_per_queue_config(recorded, queue_dir.resolve()))
    except ConfigError as exc:
        print(f"register failed: {exc}", file=sys.stderr)
        raise typer.Exit(code=2) from exc
    try:
        replaced = registry_mod.register_queue(queue_dir, config=recorded)
    except OSError as exc:
        print(f"register failed: {exc}", file=sys.stderr)
        raise typer.Exit(code=2) from exc
    queue = queue_dir.resolve()
    print(f"registered: {queue}")
    if recorded is not None:
        print(f"config: {recorded}")
    for q in replaced:
        print(f"replaced: {q}")
    note = registry_mod.handover_note(queue, replaced)
    if note is not None:
        print(note)


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

    The watchdog manages the last one. A registry written before
    ``watchdog register`` replaced its entry can list more; the watchdog
    ignores those, and a warning on stderr names them. Another warns
    when the managed queue is not an existing directory, which a tick
    skips. Stdout stays one path per line.
    """
    queues = registry_mod.load_registered_queues()
    for q in queues:
        print(q)
    managed = registry_mod.managed_queue(queues)
    ignored = registry_mod.ignored_queues(queues)
    if ignored:
        print(
            f"warning: one supervisor runs per user, so the watchdog manages only the "
            f"last queue, {managed}, and ignores {', '.join(str(q) for q in ignored)}. "
            "To register just one, run: claude-task-runner watchdog register --queue <queue>",
            file=sys.stderr,
        )
    if managed is not None and not os.path.isdir(managed):
        print(
            f"warning: {managed} is not an existing directory, so the watchdog skips it. "
            "To stop managing it, run: claude-task-runner watchdog unregister "
            f"--queue {managed}",
            file=sys.stderr,
        )
