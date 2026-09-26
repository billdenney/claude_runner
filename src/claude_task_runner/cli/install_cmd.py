"""``claude-task-runner install`` and ``install uninstall`` — wire up
or remove the watchdog (systemd or cron) with operator confirmation.

Per ADR-0014, every cutoff is configurable, but the *interactive*
nature of install (TTY confirmation, ``crontab -`` invocation,
``systemctl`` call) makes this the I/O bookend to the pure planning
modules in :mod:`cron.install` / :mod:`cron.systemd_unit`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import typer
from rich.console import Console
from rich.prompt import Confirm

from claude_task_runner.cli._helpers import (
    CWD_DEFAULT_LABEL,
    require_queue_option,
    resolve_per_queue_config,
)
from claude_task_runner.clock import RealClock
from claude_task_runner.config.loader import load_settings
from claude_task_runner.cron import install as cron_install
from claude_task_runner.cron import registry as registry_mod
from claude_task_runner.cron import systemd_unit as systemd_mod

app = typer.Typer(no_args_is_help=False, invoke_without_command=False, rich_markup_mode=None)


def _watchdog_script_path() -> Path:
    """Resolve the absolute path to the packaged ``watchdog.sh``."""
    return Path(__file__).resolve().parent.parent / "cron" / "watchdog.sh"


def _supervisor_command(queue_dir: Path, config: Path | None = None) -> str:
    """Build the absolute command line systemd should invoke.

    Uses ``shutil.which`` so the ExecStart= line is fully-qualified
    (systemd does not search PATH by default for user units).

    When the operator passes ``--config`` to ``install``, propagate it
    into the ExecStart line so the supervisor that systemd launches
    reads the same per-queue TOML the operator validated against.
    Previously the ``--config`` flag was accepted by ``install`` but
    dropped on the floor, leaving the supervisor to fall back to
    defaults (e.g. wrong ``config_dir`` -> wrong Claude account).
    ``config`` must be absolute, as ``install`` makes it: the unit runs
    with ``WorkingDirectory=<queue>``, where a relative path would name
    a file under the queue rather than the one ``install`` checked.
    """
    exe = shutil.which("claude-task-runner")
    if exe is None:
        raise typer.Exit(
            code=2,
        ) from RuntimeError("claude-task-runner not found on PATH; is the package installed?")
    cmd = f"{exe} supervisor start --queue {queue_dir}"
    if config is not None:
        cmd += f" --config {config}"
    return cmd


def _detect_init_system(preferred: str) -> str:
    """Decide whether to use systemd or cron.

    ``preferred`` is the operator's ``[supervisor].preferred_init_system``
    setting: ``"auto"``, ``"systemd"``, or ``"cron"``.
    """
    if preferred == "systemd":
        return "systemd"
    if preferred == "cron":
        return "cron"
    # auto-detect
    if systemd_mod.is_systemd_user_available():
        return "systemd"
    return "cron"


@app.callback(invoke_without_command=True)
def install(
    ctx: typer.Context,
    *,
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Per-queue claude_runner.toml."
    ),
    queue_dir: Path = typer.Option(
        Path.cwd,
        "--queue",
        help="Queue directory the supervisor should manage.",
        show_default=CWD_DEFAULT_LABEL,
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the y/N confirmation."),
) -> None:
    """Install the supervisor watchdog (systemd preferred, cron fallback).

    Auto-detects which init system to use based on
    ``[supervisor].preferred_init_system`` (default ``auto``). Shows
    the proposed change and asks for confirmation before writing.

    systemd: writes a ``--user`` unit that runs the supervisor for
    ``--queue`` and restarts it when it fails. The queue's
    ``[watchdog]`` sets the unit's restart policy, so re-run ``install``
    after changing it.

    cron: adds a crontab line that runs ``watchdog tick`` every minute
    and registers ``--queue`` in ``~/.claude_task_runner/queues.json``.
    A tick restarts the supervisor of each registered queue that is not
    running, even one stopped with ``supervisor stop`` or ``drain``,
    and backs off after repeated crashes.
    """
    if ctx.invoked_subcommand is not None:
        return  # Subcommand handles itself.

    console = Console()
    # Before any plan is shown: a queue that is not there would be recreated,
    # by the unit's supervisor under systemd or by the next tick under cron.
    queue_path = require_queue_option(queue_dir, console)
    resolved_config = resolve_per_queue_config(config, queue_path)
    if resolved_config is not None:
        # Absolute, naming the file loaded below: the unit runs with
        # WorkingDirectory=<queue>, where a relative path names another.
        resolved_config = resolved_config.absolute()
    settings = load_settings(resolved_config)

    init_system = _detect_init_system(settings.supervisor.preferred_init_system)
    console.print(
        f"[bold]Detected init system:[/] {init_system} "
        f"(setting: {settings.supervisor.preferred_init_system})"
    )

    if init_system == "systemd":
        # No watchdog registration here: systemd restarts the unit
        # itself, and nothing on this path runs `watchdog tick`.
        # Registering would only matter if a cron block were also
        # installed, and then the tick would restart a supervisor that
        # `Restart=on-failure` deliberately left stopped, outside the
        # unit's control.
        try:
            sd_plan = systemd_mod.build_install_plan(
                supervisor_command=_supervisor_command(queue_path, resolved_config),
                queue_dir=queue_path,
                # The queue's [watchdog] sets RestartSec, StartLimitBurst
                # and StartLimitIntervalSec.
                watchdog=settings.watchdog,
                # ADR-0025: generate fast-stop wiring when adoption is on so
                # the unit's ExecStop / TimeoutStopSec match runtime behaviour.
                adopt_workers=settings.supervisor.adopt_workers,
            )
        except systemd_mod.UnitSettingError as exc:
            # Without markup, so the "[watchdog]" in the message is printed.
            console.print(
                f"systemd install failed: {exc}. Nothing was written.",
                style="bold red",
                markup=False,
                highlight=False,
                soft_wrap=True,
            )
            raise typer.Exit(code=2) from exc
        verb = "replace" if sd_plan.block_existed else "create"
        console.print(f"\n[bold]Will {verb} systemd user unit at:[/]\n  {sd_plan.unit_path}\n")
        console.print("[bold]Unit text:[/]")
        for line in sd_plan.unit_text.splitlines():
            console.print(f"  {line}")
        console.print(f"\n[bold]Then run:[/] {' '.join(sd_plan.enable_command)}\n")
        if not yes and not Confirm.ask("Apply this change?", default=False):
            console.print("[yellow]Aborted.[/]")
            raise typer.Exit(code=1)
        try:
            systemd_mod.apply_plan(sd_plan)
        except systemd_mod.SystemdError as exc:
            console.print(f"[bold red]systemd install failed:[/] {exc}")
            raise typer.Exit(code=2) from exc
        console.print("[green]systemd unit installed and started.[/]")
        return

    # cron path
    cron_plan = cron_install.build_install_plan(watchdog_path=_watchdog_script_path())
    verb = "replace" if cron_plan.block_existed else "add"
    console.print(f"\n[bold]Will {verb} the managed block in your crontab:[/]\n")
    if cron_plan.diff_lines:
        for line in cron_plan.diff_lines:
            color = "green" if line.startswith("+") else "red"
            console.print(f"  [{color}]{line}[/]")
    else:
        console.print("  [dim](no visible diff — block already up to date)[/]")
    # The crontab line runs `watchdog tick` with no --queue, and a tick
    # manages only the queue in this registry.
    registry = registry_mod.queues_registry_path()
    console.print(f"\n[bold]Will register this queue with the watchdog in {registry}:[/]")
    console.print(f"  {queue_path}")
    _show_replaced_queues(console, queue_path)
    if not yes and not Confirm.ask("\nApply this change?", default=False):
        console.print("[yellow]Aborted.[/]")
        raise typer.Exit(code=1)

    # Register before touching the crontab, so a failed registry write
    # leaves nothing changed. The other order could leave a cron line
    # whose ticks have no queue to manage.
    try:
        replaced = registry_mod.register_queue(queue_path)
    except OSError as exc:
        console.print(f"[bold red]watchdog registration failed:[/] {exc}")
        raise typer.Exit(code=2) from exc
    console.print(f"[green]Registered {queue_path} with the watchdog.[/]")

    backup = cron_install.backup_crontab(cron_plan.existing_text, clock=RealClock())
    console.print(f"[dim]Backed up existing crontab to {backup}[/]")
    try:
        cron_install.apply_plan(cron_plan)
    except cron_install.CrontabError as exc:
        console.print(f"[bold red]crontab install failed:[/] {exc}")
        raise typer.Exit(code=2) from exc
    console.print("[green]crontab updated.[/]")
    note = registry_mod.handover_note(queue_path, replaced)
    if note is not None:
        console.print(note, markup=False, highlight=False, soft_wrap=True)


def _show_replaced_queues(console: Console, queue: Path) -> None:
    """List the queues that registering ``queue`` replaces, before the y/N prompt.

    One supervisor runs per user, so the watchdog manages one queue, and
    registering one replaces whatever the registry lists now. Read
    without side effects: a corrupt registry is only reported here, and
    ``register_queue`` keeps a copy of it as ``queues.json.broken``.
    Paths are printed without Rich markup, so a ``[`` stays as typed."""
    try:
        current = registry_mod.read_registered_queues()
    except registry_mod.RegistryError as exc:
        console.print(
            f"It replaces the registry, which is unreadable ({exc}); "
            "a copy is kept as queues.json.broken.",
            markup=False,
            highlight=False,
            soft_wrap=True,
        )
        return
    replaced = [q for q in dict.fromkeys(current) if q != queue]
    if not replaced:
        return
    console.print("[bold]It replaces, since the watchdog manages one queue:[/]")
    for q in replaced:
        console.print(f"  {q}", markup=False, highlight=False, soft_wrap=True)


@app.command("uninstall")
def uninstall(
    *,
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Per-queue claude_runner.toml."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the y/N confirmation."),
) -> None:
    """Remove the watchdog installation (systemd unit AND/OR cron block).

    Leaves ``~/.claude_task_runner/queues.json`` as it is. Once no cron
    block is installed, lists the queues it still holds with the
    ``watchdog unregister`` command for each. No tick reads the registry
    without the cron block, and a later cron ``install`` replaces it
    with its own queue.
    """
    settings = load_settings(config)
    console = Console()

    init_system = _detect_init_system(settings.supervisor.preferred_init_system)

    # Always offer to remove cron block — it's harmless if absent.
    console.print("[bold]Uninstalling watchdog:[/] both systemd and cron will be checked.")

    # systemd
    if init_system == "systemd":
        unit_path = systemd_mod.systemd_unit_path()
        if unit_path.exists():
            console.print(f"[bold]systemd unit:[/] {unit_path} — will be removed")
            if not yes and not Confirm.ask("Remove systemd unit?", default=False):
                console.print("[yellow]systemd uninstall skipped.[/]")
            else:
                removed = systemd_mod.uninstall()
                if removed:
                    console.print("[green]systemd unit removed.[/]")
        else:
            console.print("[dim]No systemd unit installed.[/]")

    # cron
    try:
        cron_plan = cron_install.build_uninstall_plan()
    except cron_install.CrontabError as exc:
        console.print(f"[dim]No crontab access ({exc}); skipping cron.[/]")
        return

    if not cron_plan.block_existed:
        console.print("[dim]No managed block in crontab; nothing to remove there.[/]")
        _report_registered_queues(console)
        return

    console.print("\n[bold]crontab change:[/]")
    for line in cron_plan.diff_lines:
        color = "green" if line.startswith("+") else "red"
        console.print(f"  [{color}]{line}[/]")
    if not yes and not Confirm.ask("\nRemove the cron block?", default=False):
        console.print("[yellow]cron uninstall skipped.[/]")
        return

    backup = cron_install.backup_crontab(cron_plan.existing_text, clock=RealClock())
    console.print(f"[dim]Backed up existing crontab to {backup}[/]")
    try:
        cron_install.apply_plan(cron_plan)
    except cron_install.CrontabError as exc:
        console.print(f"[bold red]cron uninstall failed:[/] {exc}")
        raise typer.Exit(code=2) from exc
    console.print("[green]crontab block removed.[/]")
    _report_registered_queues(console)


def _report_registered_queues(console: Console) -> None:
    """List what ``queues.json`` still holds once no cron block is installed.

    ``uninstall`` leaves the registry alone. No tick reads it without the
    cron block, and a later cron ``install`` replaces it with its own
    queue, so this is for an operator who wants it gone now. Printed
    without Rich markup, so a ``[`` in a path stays as typed. A corrupt
    registry is reported and left as it is; the uninstall itself has
    already succeeded."""
    registry = registry_mod.queues_registry_path()
    try:
        queues = registry_mod.read_registered_queues()
    except registry_mod.RegistryError as exc:
        console.print(
            f"warning: {exc}; uninstall left it as it is.",
            style="yellow",
            markup=False,
            highlight=False,
            soft_wrap=True,
        )
        return
    if not queues:
        return
    count, them = ("1 queue", "it") if len(queues) == 1 else (f"{len(queues)} queues", "them")
    console.print(
        f"{registry} still lists {count}. No tick reads it without the cron block, and "
        f"a later cron install replaces the list with its own queue. To drop {them} now:",
        markup=False,
        highlight=False,
        soft_wrap=True,
    )
    for q in queues:
        console.print(
            f"  claude-task-runner watchdog unregister --queue {q}",
            markup=False,
            highlight=False,
            soft_wrap=True,
        )
