"""``claude-task-runner install`` and ``uninstall`` — wire up the
watchdog (systemd or cron) with operator confirmation.

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

from claude_task_runner.clock import RealClock
from claude_task_runner.config.loader import load_settings
from claude_task_runner.cron import install as cron_install
from claude_task_runner.cron import systemd_unit as systemd_mod

app = typer.Typer(no_args_is_help=False, invoke_without_command=False)


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
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the y/N confirmation."),
) -> None:
    """Install the supervisor watchdog (systemd preferred, cron fallback).

    Auto-detects which init system to use based on
    ``[supervisor].preferred_init_system`` (default ``auto``). Shows
    the proposed change and asks for confirmation before writing.
    """
    if ctx.invoked_subcommand is not None:
        return  # Subcommand handles itself.

    settings = load_settings(config)
    console = Console()
    queue_path = queue_dir.resolve()

    init_system = _detect_init_system(settings.supervisor.preferred_init_system)
    console.print(
        f"[bold]Detected init system:[/] {init_system} "
        f"(setting: {settings.supervisor.preferred_init_system})"
    )

    if init_system == "systemd":
        sd_plan = systemd_mod.build_install_plan(
            supervisor_command=_supervisor_command(queue_path, config),
            queue_dir=queue_path,
        )
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
    if not yes and not Confirm.ask("\nApply this change?", default=False):
        console.print("[yellow]Aborted.[/]")
        raise typer.Exit(code=1)

    backup = cron_install.backup_crontab(cron_plan.existing_text, clock=RealClock())
    console.print(f"[dim]Backed up existing crontab to {backup}[/]")
    try:
        cron_install.apply_plan(cron_plan)
    except cron_install.CrontabError as exc:
        console.print(f"[bold red]crontab install failed:[/] {exc}")
        raise typer.Exit(code=2) from exc
    console.print("[green]crontab updated.[/]")


@app.command("uninstall")
def uninstall(
    *,
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Per-queue claude_runner.toml."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the y/N confirmation."),
) -> None:
    """Remove the watchdog installation (systemd unit AND/OR cron block)."""
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
