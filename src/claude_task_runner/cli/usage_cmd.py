"""``claude-task-runner usage`` subcommands.

* ``render`` (default) — print a colored snapshot.
* ``json`` — print one-line JSON: ``{"five_hour":..., "seven_day":...}``.
* ``healthcheck`` — capture+parse; exit non-zero on drift / timeout.
* ``capture`` — capture only; save the raw .cap to a path.
* ``parse-file`` — parse a previously-saved .cap and print the result.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console

from claude_task_runner.clock import RealClock
from claude_task_runner.config.loader import load_settings
from claude_task_runner.usage import capture as capture_mod
from claude_task_runner.usage import parser as parser_mod
from claude_task_runner.usage import whoami as whoami_mod
from claude_task_runner.usage.drift import (
    UsageCaptureSpawnError,
    UsageCaptureTimeout,
    UsageFormatDrift,
)

app = typer.Typer(no_args_is_help=False, invoke_without_command=True)

EXIT_OK = 0
EXIT_PARSE_DRIFT = 1
EXIT_CAPTURE_TIMEOUT = 2
EXIT_SPAWN_ERROR = 3
EXIT_UNEXPECTED = 4


def _default_captures_dir() -> Path:
    return Path.home() / ".claude_task_runner" / "usage_captures"


def _bar(pct: int) -> str:
    filled = pct // 5
    empty = 20 - filled
    return "█" * filled + "░" * empty


@app.callback()
def _root(
    ctx: typer.Context,
    config: Path | None = typer.Option(
        None,
        "--config",
        "-c",
        help="Per-queue claude_runner.toml. Defaults to package settings.",
    ),
) -> None:
    """If no subcommand given, behave like ``render``."""
    settings = load_settings(config)
    ctx.ensure_object(dict)
    ctx.obj["settings"] = settings
    ctx.obj["captures_dir"] = _default_captures_dir()
    if ctx.invoked_subcommand is None:
        ctx.invoke(render)


@app.command("render")
def render(ctx: typer.Context) -> None:
    """Print a colored snapshot of the current 5h and 7d windows."""
    settings = ctx.obj["settings"]
    captures_dir: Path = ctx.obj["captures_dir"]
    console = Console()
    clock = RealClock()
    try:
        raw, _path = capture_mod.capture(
            settings.usage,
            clock,
            captures_dir=captures_dir,
            claude_executable=settings.claude.executable,
            claude_config_dir=settings.claude.config_dir,
        )
        reading = parser_mod.parse(raw, clock.now(), clock)
    except UsageCaptureSpawnError as exc:
        console.print(f"[bold red]spawn error:[/] {exc}")
        raise typer.Exit(code=EXIT_SPAWN_ERROR) from exc
    except UsageCaptureTimeout as exc:
        console.print(f"[bold red]capture timeout:[/] {exc}")
        raise typer.Exit(code=EXIT_CAPTURE_TIMEOUT) from exc
    except UsageFormatDrift as exc:
        console.print(f"[bold red]format drift:[/] {exc}")
        raise typer.Exit(code=EXIT_PARSE_DRIFT) from exc

    console.print(
        f"[bold]Claude Code Usage[/]  [dim]{clock.now().strftime('%Y-%m-%d %H:%M:%S UTC')}[/]"
    )
    for label, window in (
        ("5-hour session", reading.five_hour),
        ("7-day weekly", reading.seven_day),
    ):
        pct = window.utilization_pct
        color = "green" if pct < 70 else ("yellow" if pct < 90 else "red")
        bar = _bar(pct)
        console.print(f"  [bold]{label:<16}[/] [{color}]{bar}[/] {pct:>3}%")
        if window.resets_at_raw:
            console.print(f"  [dim]Resets {window.resets_at_raw}[/]")
    for w in reading.extra_windows:
        pct = w.utilization_pct
        color = "green" if pct < 70 else ("yellow" if pct < 90 else "red")
        bar = _bar(pct)
        label = f"7-day {w.label}"
        console.print(f"  [bold]{label:<24}[/] [{color}]{bar}[/] {pct:>3}%")
        if w.resets_at_raw:
            console.print(f"  [dim]Resets {w.resets_at_raw}[/]")


@app.command("json")
def to_json(ctx: typer.Context) -> None:
    """Print machine-readable JSON of the current windows."""
    settings = ctx.obj["settings"]
    captures_dir: Path = ctx.obj["captures_dir"]
    clock = RealClock()
    try:
        raw, _path = capture_mod.capture(
            settings.usage,
            clock,
            captures_dir=captures_dir,
            claude_executable=settings.claude.executable,
            claude_config_dir=settings.claude.config_dir,
        )
        reading = parser_mod.parse(raw, clock.now(), clock)
    except (UsageCaptureSpawnError, UsageCaptureTimeout) as exc:
        sys.stderr.write(f"capture error: {exc}\n")
        raise typer.Exit(code=EXIT_CAPTURE_TIMEOUT) from exc
    except UsageFormatDrift as exc:
        sys.stderr.write(f"format drift: {exc}\n")
        raise typer.Exit(code=EXIT_PARSE_DRIFT) from exc

    payload = {
        "five_hour": {
            "utilization": reading.five_hour.utilization_pct,
            "resets_at_raw": reading.five_hour.resets_at_raw,
            "resets_at": (
                reading.five_hour.resets_at.isoformat() if reading.five_hour.resets_at else None
            ),
        },
        "seven_day": {
            "utilization": reading.seven_day.utilization_pct,
            "resets_at_raw": reading.seven_day.resets_at_raw,
            "resets_at": (
                reading.seven_day.resets_at.isoformat() if reading.seven_day.resets_at else None
            ),
        },
        "extra_windows": [
            {
                "label": w.label,
                "utilization": w.utilization_pct,
                "resets_at_raw": w.resets_at_raw,
                "resets_at": w.resets_at.isoformat() if w.resets_at else None,
            }
            for w in reading.extra_windows
        ],
    }
    print(json.dumps(payload))


@app.command("healthcheck")
def healthcheck(ctx: typer.Context) -> None:
    """Capture+parse and report PASS/FAIL/WARN. Exit code signals state.

    Exit codes:
      0  clean
      1  parse drift
      2  capture timeout
      3  spawn error
      4  unexpected
    """
    settings = ctx.obj["settings"]
    captures_dir: Path = ctx.obj["captures_dir"]
    console = Console()
    clock = RealClock()
    try:
        raw, _path = capture_mod.capture(
            settings.usage,
            clock,
            captures_dir=captures_dir,
            claude_executable=settings.claude.executable,
            claude_config_dir=settings.claude.config_dir,
        )
        reading = parser_mod.parse(raw, clock.now(), clock)
    except UsageCaptureSpawnError as exc:
        console.print(f"[bold red]FAIL[/] spawn error: {exc}")
        raise typer.Exit(code=EXIT_SPAWN_ERROR) from exc
    except UsageCaptureTimeout as exc:
        console.print(f"[bold red]FAIL[/] capture timeout: {exc}")
        raise typer.Exit(code=EXIT_CAPTURE_TIMEOUT) from exc
    except UsageFormatDrift as exc:
        console.print(f"[bold red]FAIL[/] parser drift: {exc}")
        raise typer.Exit(code=EXIT_PARSE_DRIFT) from exc
    except Exception as exc:
        console.print(f"[bold red]FAIL[/] unexpected: {exc}")
        raise typer.Exit(code=EXIT_UNEXPECTED) from exc

    warns: list[str] = []
    if reading.five_hour.resets_at is None:
        warns.append("5h resets_at unparseable")
    if reading.seven_day.resets_at is None:
        warns.append("weekly resets_at unparseable")

    if warns:
        console.print(f"[bold yellow]WARN[/] parse OK; {', '.join(warns)}")
    else:
        console.print(
            f"[bold green]PASS[/] 5h={reading.five_hour.utilization_pct}% "
            f"weekly={reading.seven_day.utilization_pct}%"
        )


@app.command("capture")
def capture_only(
    ctx: typer.Context,
    save: Path = typer.Option(
        ...,
        "--save",
        "-o",
        help="Where to write the raw .cap file.",
    ),
) -> None:
    """Capture raw `claude /usage` output to a file (no parsing)."""
    settings = ctx.obj["settings"]
    captures_dir: Path = ctx.obj["captures_dir"]
    clock = RealClock()
    try:
        raw, _ = capture_mod.capture(
            settings.usage,
            clock,
            captures_dir=captures_dir,
            claude_executable=settings.claude.executable,
            claude_config_dir=settings.claude.config_dir,
        )
    except UsageCaptureSpawnError as exc:
        sys.stderr.write(f"spawn error: {exc}\n")
        raise typer.Exit(code=EXIT_SPAWN_ERROR) from exc
    except UsageCaptureTimeout as exc:
        sys.stderr.write(f"capture timeout: {exc}\n")
        raise typer.Exit(code=EXIT_CAPTURE_TIMEOUT) from exc
    save.parent.mkdir(parents=True, exist_ok=True)
    save.write_bytes(raw)
    print(f"saved {len(raw)} bytes to {save}")


@app.command("whoami")
def whoami(
    ctx: typer.Context,
    *,
    quick: bool = typer.Option(
        False,
        "--quick",
        help="Skip the TUI capture; report only credentials.json fields.",
    ),
) -> None:
    """Show which Claude account this `[claude].config_dir` is using.

    Reads ``credentials.json`` for ``subscriptionType`` /
    ``rateLimitTier`` and (unless ``--quick``) does a one-shot
    ``claude /usage`` capture to extract the welcome panel's
    organization label. Useful before trusting any usage numbers — it
    confirms which account is being read.
    """
    settings = ctx.obj["settings"]
    captures_dir: Path = ctx.obj["captures_dir"]
    console = Console()

    if quick:
        snap = whoami_mod.from_credentials_only(settings.claude.config_dir)
    else:
        clock = RealClock()
        try:
            raw, _ = capture_mod.capture(
                settings.usage,
                clock,
                captures_dir=captures_dir,
                claude_executable=settings.claude.executable,
                claude_config_dir=settings.claude.config_dir,
            )
        except UsageCaptureSpawnError as exc:
            console.print(f"[bold red]spawn error:[/] {exc}")
            raise typer.Exit(code=EXIT_SPAWN_ERROR) from exc
        except UsageCaptureTimeout as exc:
            console.print(
                f"[bold yellow]capture timeout:[/] {exc}\n"
                "Falling back to credentials-only identity."
            )
            snap = whoami_mod.from_credentials_only(settings.claude.config_dir)
        else:
            snap = whoami_mod.from_capture(raw, settings.claude.config_dir)

    config_label = snap.config_dir or "<default ~/.claude>"
    console.print(f"[bold]Config dir:[/]      {config_label}")
    console.print(f"[bold]Subscription:[/]    {snap.subscription_type or '[dim](unknown)[/]'}")
    console.print(f"[bold]Rate limit:[/]      {snap.rate_limit_tier or '[dim](unknown)[/]'}")
    if snap.welcome_label:
        console.print(f"[bold]Org / account:[/]   {snap.welcome_label}")
    if snap.scopes:
        console.print(f"[bold]Scopes:[/]          {', '.join(snap.scopes)}")
    if snap.is_team():
        console.print("[bold cyan]Account class:[/]   Team / Enterprise")
    elif snap.is_personal():
        console.print("[bold cyan]Account class:[/]   Personal (Pro / Max)")
    else:
        console.print(
            "[bold yellow]Account class:[/]   unknown — verify before relying on usage numbers"
        )


@app.command("parse-file")
def parse_file(
    path: Path = typer.Argument(..., exists=True, readable=True),
) -> None:
    """Parse a previously-saved .cap file and print the result.

    Useful for reproducing drift incidents and adding new fixtures.
    """
    raw = path.read_bytes()
    clock = RealClock()
    try:
        reading = parser_mod.parse(raw, datetime.now(UTC), clock)
    except UsageFormatDrift as exc:
        print(json.dumps({"error": "format_drift", "message": str(exc)}))
        raise typer.Exit(code=EXIT_PARSE_DRIFT) from exc

    payload = {
        "five_hour": {
            "utilization": reading.five_hour.utilization_pct,
            "resets_at_raw": reading.five_hour.resets_at_raw,
        },
        "seven_day": {
            "utilization": reading.seven_day.utilization_pct,
            "resets_at_raw": reading.seven_day.resets_at_raw,
        },
    }
    print(json.dumps(payload, indent=2))
