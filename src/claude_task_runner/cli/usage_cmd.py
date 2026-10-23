"""``claude-task-runner usage`` subcommands.

* ``render`` (default) — print a colored snapshot.
* ``json`` — print one-line JSON: ``{"five_hour":..., "seven_day":...}``.
* ``healthcheck`` — capture+parse; exit non-zero on drift / timeout.
* ``capture`` — capture only; save the raw .cap to a path.
* ``parse-file`` — parse a previously-saved .cap and print the result.
* ``whoami`` — show which Claude account an account's ``config_dir`` reads.
* ``refresh`` — refresh the OAuth token of every configured account.

``--config`` and ``--account`` belong to the ``usage`` group, not to its
subcommands, so they go before the subcommand:
``claude-task-runner usage --config <queue>/claude_runner.toml --account work whoami``.

Every subcommand but ``refresh`` and ``parse-file`` reads one account: the
queue's only ``[[accounts]]`` entry (with none, the ``default`` account the
loader makes from ``[claude].config_dir``), or the one ``--account`` names.
A queue with several needs ``--account``, as the supervisor reads each
account from its own ``config_dir`` and ``[claude].config_dir`` is then
ignored.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console

from claude_task_runner.cli._helpers import literal, load_settings_or_exit
from claude_task_runner.clock import RealClock
from claude_task_runner.config.schema import AccountSettings, Settings
from claude_task_runner.usage import capture as capture_mod
from claude_task_runner.usage import parser as parser_mod
from claude_task_runner.usage import whoami as whoami_mod
from claude_task_runner.usage.drift import (
    UsageCaptureSpawnError,
    UsageCaptureTimeout,
    UsageFormatDrift,
)

app = typer.Typer(no_args_is_help=False, invoke_without_command=True, rich_markup_mode=None)

EXIT_OK = 0
EXIT_PARSE_DRIFT = 1
EXIT_CAPTURE_TIMEOUT = 2
EXIT_SPAWN_ERROR = 3
EXIT_UNEXPECTED = 4


def _default_captures_dir() -> Path:
    return Path.home() / ".claude_task_runner" / "usage_captures"


def _named_account(settings: Settings, name: str) -> AccountSettings:
    """The ``[[accounts]]`` entry called ``name``; a usage error (exit 2)
    listing the configured names when there is none."""
    for acct in settings.accounts:
        if acct.name == name:
            return acct
    raise typer.BadParameter(
        f"no account {name!r} in [[accounts]]; configured: "
        + ", ".join(a.name for a in settings.accounts),
        param_hint="'--account'",
    )


def _account(ctx: typer.Context) -> AccountSettings:
    """The one account a subcommand reads: ``--account``'s, or the only one.

    With several accounts and no ``--account`` it is a usage error (exit
    2) naming them, rather than a guess: ``[claude].config_dir`` is
    ignored once ``[[accounts]]`` is declared, and the supervisor reads
    each account from its own ``config_dir``.
    """
    settings: Settings = ctx.obj["settings"]
    name: str | None = ctx.obj["account"]
    if name is not None:
        return _named_account(settings, name)
    if len(settings.accounts) == 1:
        return settings.accounts[0]
    raise typer.BadParameter(
        f"the queue declares {len(settings.accounts)} accounts ("
        + ", ".join(a.name for a in settings.accounts)
        + "); name the one to read with --account",
        param_hint="'--account'",
    )


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
    account: str | None = typer.Option(
        None,
        "--account",
        help=(
            "The [[accounts]] entry to read. Needed when the queue declares "
            "several; refresh then refreshes only that one."
        ),
    ),
) -> None:
    """Usage capture, parse, and drift check.

    With no subcommand, runs ``render``.
    """
    settings = load_settings_or_exit(config, Console())
    if account is not None:
        _named_account(settings, account)
    ctx.ensure_object(dict)
    ctx.obj["settings"] = settings
    ctx.obj["account"] = account
    ctx.obj["captures_dir"] = _default_captures_dir()
    if ctx.invoked_subcommand is None:
        # Click calls a plain function with only the arguments given, so
        # render gets this context explicitly.
        ctx.invoke(render, ctx)


@app.command("render")
def render(ctx: typer.Context) -> None:
    """Print a colored snapshot of the current 5h and 7d windows."""
    settings = ctx.obj["settings"]
    config_dir = _account(ctx).config_dir
    captures_dir: Path = ctx.obj["captures_dir"]
    console = Console()
    clock = RealClock()
    try:
        raw, _path = capture_mod.capture(
            settings.usage,
            clock,
            captures_dir=captures_dir,
            claude_executable=settings.claude.executable,
            claude_config_dir=config_dir,
        )
        reading = parser_mod.parse(raw, clock.now(), clock)
    except UsageCaptureSpawnError as exc:
        console.print(literal(("spawn error:", "bold red"), " ", exc))
        raise typer.Exit(code=EXIT_SPAWN_ERROR) from exc
    except UsageCaptureTimeout as exc:
        console.print(literal(("capture timeout:", "bold red"), " ", exc))
        raise typer.Exit(code=EXIT_CAPTURE_TIMEOUT) from exc
    except UsageFormatDrift as exc:
        console.print(literal(("format drift:", "bold red"), " ", exc))
        raise typer.Exit(code=EXIT_PARSE_DRIFT) from exc

    console.print(
        literal(
            ("Claude Code Usage", "bold"),
            "  ",
            (clock.now().strftime("%Y-%m-%d %H:%M:%S UTC"), "dim"),
        )
    )
    for label, window in (
        ("5-hour session", reading.five_hour),
        ("7-day weekly", reading.seven_day),
    ):
        pct = window.utilization_pct
        color = "green" if pct < 70 else ("yellow" if pct < 90 else "red")
        bar = _bar(pct)
        console.print(literal("  ", (f"{label:<16}", "bold"), " ", (bar, color), f" {pct:>3}%"))
        if window.resets_at_raw:
            console.print(literal("  ", (f"Resets {window.resets_at_raw}", "dim")))
    for w in reading.extra_windows:
        pct = w.utilization_pct
        color = "green" if pct < 70 else ("yellow" if pct < 90 else "red")
        bar = _bar(pct)
        label = f"7-day {w.label}"
        console.print(literal("  ", (f"{label:<24}", "bold"), " ", (bar, color), f" {pct:>3}%"))
        if w.resets_at_raw:
            console.print(literal("  ", (f"Resets {w.resets_at_raw}", "dim")))


@app.command("json")
def to_json(ctx: typer.Context) -> None:
    """Print machine-readable JSON of the current windows."""
    settings = ctx.obj["settings"]
    config_dir = _account(ctx).config_dir
    captures_dir: Path = ctx.obj["captures_dir"]
    clock = RealClock()
    try:
        raw, _path = capture_mod.capture(
            settings.usage,
            clock,
            captures_dir=captures_dir,
            claude_executable=settings.claude.executable,
            claude_config_dir=config_dir,
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

    \b
    Exit codes:
      0  clean
      1  parse drift
      2  capture timeout, or a usage error such as a queue of several
         accounts without --account (the message says which)
      3  spawn error
      4  unexpected
    """
    settings = ctx.obj["settings"]
    config_dir = _account(ctx).config_dir
    captures_dir: Path = ctx.obj["captures_dir"]
    console = Console()
    clock = RealClock()
    try:
        raw, _path = capture_mod.capture(
            settings.usage,
            clock,
            captures_dir=captures_dir,
            claude_executable=settings.claude.executable,
            claude_config_dir=config_dir,
        )
        reading = parser_mod.parse(raw, clock.now(), clock)
    except UsageCaptureSpawnError as exc:
        console.print(literal(("FAIL", "bold red"), f" spawn error: {exc}"))
        raise typer.Exit(code=EXIT_SPAWN_ERROR) from exc
    except UsageCaptureTimeout as exc:
        console.print(literal(("FAIL", "bold red"), f" capture timeout: {exc}"))
        raise typer.Exit(code=EXIT_CAPTURE_TIMEOUT) from exc
    except UsageFormatDrift as exc:
        console.print(literal(("FAIL", "bold red"), f" parser drift: {exc}"))
        raise typer.Exit(code=EXIT_PARSE_DRIFT) from exc
    except Exception as exc:
        console.print(literal(("FAIL", "bold red"), f" unexpected: {exc}"))
        raise typer.Exit(code=EXIT_UNEXPECTED) from exc

    warns: list[str] = []
    if reading.five_hour.resets_at is None:
        warns.append("5h resets_at unparseable")
    if reading.seven_day.resets_at is None:
        warns.append("weekly resets_at unparseable")

    if warns:
        console.print(literal(("WARN", "bold yellow"), f" parse OK; {', '.join(warns)}"))
    else:
        console.print(
            literal(
                ("PASS", "bold green"),
                f" 5h={reading.five_hour.utilization_pct}% "
                f"weekly={reading.seven_day.utilization_pct}%",
            )
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
    config_dir = _account(ctx).config_dir
    captures_dir: Path = ctx.obj["captures_dir"]
    clock = RealClock()
    try:
        raw, _ = capture_mod.capture(
            settings.usage,
            clock,
            captures_dir=captures_dir,
            claude_executable=settings.claude.executable,
            claude_config_dir=config_dir,
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
    """Show which Claude account a config_dir is logged in to.

    The config_dir is the queue's only account's, or ``--account``'s.

    Reads ``credentials.json`` for ``subscriptionType`` /
    ``rateLimitTier`` and (unless ``--quick``) does a one-shot
    ``claude /usage`` capture to extract the welcome panel's
    organization label. Useful before trusting any usage numbers — it
    confirms which account is being read.
    """
    settings = ctx.obj["settings"]
    config_dir = _account(ctx).config_dir
    captures_dir: Path = ctx.obj["captures_dir"]
    console = Console()

    if quick:
        snap = whoami_mod.from_credentials_only(config_dir)
    else:
        clock = RealClock()
        try:
            raw, _ = capture_mod.capture(
                settings.usage,
                clock,
                captures_dir=captures_dir,
                claude_executable=settings.claude.executable,
                claude_config_dir=config_dir,
            )
        except UsageCaptureSpawnError as exc:
            console.print(literal(("spawn error:", "bold red"), " ", exc))
            raise typer.Exit(code=EXIT_SPAWN_ERROR) from exc
        except UsageCaptureTimeout as exc:
            console.print(
                literal(
                    ("capture timeout:", "bold yellow"),
                    f" {exc}\nFalling back to credentials-only identity.",
                )
            )
            snap = whoami_mod.from_credentials_only(config_dir)
        else:
            snap = whoami_mod.from_capture(raw, config_dir)

    config_label = snap.config_dir or "<default ~/.claude>"
    console.print(literal(("Config dir:", "bold"), "      ", config_label))
    console.print(
        literal(("Subscription:", "bold"), "    ", snap.subscription_type or ("(unknown)", "dim"))
    )
    console.print(
        literal(("Rate limit:", "bold"), "      ", snap.rate_limit_tier or ("(unknown)", "dim"))
    )
    if snap.welcome_label:
        console.print(literal(("Org / account:", "bold"), "   ", snap.welcome_label))
    if snap.scopes:
        console.print(literal(("Scopes:", "bold"), "          ", ", ".join(snap.scopes)))
    if snap.is_team():
        console.print("[bold cyan]Account class:[/]   Team / Enterprise")
    elif snap.is_personal():
        console.print("[bold cyan]Account class:[/]   Personal (Pro / Max)")
    else:
        console.print(
            "[bold yellow]Account class:[/]   unknown — verify before relying on usage numbers"
        )


@app.command("refresh")
def refresh(ctx: typer.Context) -> None:
    """Refresh the OAuth token of every configured account.

    With ``--account``, only that account's.

    Each refresh spawns ``claude /usage`` in a PTY against the account's
    ``config_dir``. The TUI's OAuth-backed call rewrites the credentials
    file with a fresh access token as a side effect — no operator
    interaction required, no direct OAuth endpoint dependency.

    Useful as a preflight before flipping ``[usage].source`` to
    ``"api_then_tty"``: every account's bearer is known-fresh, so the
    first hour of API-mode polls don't all fall through to TTY.

    Exit code 0 on full success; non-zero if any account failed. Each
    account's status is printed regardless of success/failure of the
    others — failures don't abort the whole run.
    """
    settings = ctx.obj["settings"]
    captures_dir: Path = ctx.obj["captures_dir"]
    console = Console()
    clock = RealClock()

    if not settings.accounts:
        console.print("[dim]no [[accounts]] configured — nothing to refresh[/]")
        return

    # Local import keeps the CLI module's import graph slim — the
    # refresh helper transitively pulls in pexpect via capture.py.
    from claude_task_runner.usage.oauth_refresh import refresh_all_accounts

    # The callback has refused an --account that names no account.
    accounts = [a for a in settings.accounts if ctx.obj["account"] in (None, a.name)]
    results = refresh_all_accounts(
        accounts=accounts,
        settings=settings.usage,
        captures_dir=captures_dir,
        clock=clock,
        claude_executable=settings.claude.executable,
    )

    ok = sum(1 for r in results if r.success)
    fail = len(results) - ok
    for r in results:
        marker = ("PASS", "bold green") if r.success else ("FAIL", "bold red")
        console.print(
            literal("  ", marker, f" {r.account} ({r.config_dir or '~/.claude'}): {r.detail}")
        )
    console.print(
        literal(
            "\n",
            ("Summary:", "bold"),
            f" {ok} refreshed, {fail} failed of {len(results)} accounts.",
        )
    )
    if fail:
        raise typer.Exit(code=EXIT_CAPTURE_TIMEOUT)


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
