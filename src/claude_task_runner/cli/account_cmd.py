"""``claude-task-runner account`` — operator surface for multi-account dispatch.

Subcommands:

* ``account list``   — show resolved accounts (queue-side decl + per-
  account policy) plus current per-account state (5h util, weekly util,
  in-flight count, paused?).
* ``account pause``  — create the account's pause marker; the dispatcher
  skips the account until ``resume``.
* ``account resume`` — remove the marker.

Skills do not import schemas directly; they invoke
``account list --json`` and parse the output.

Pause / resume write only the marker files in
``<queue>/.claude_task_runner/account_paused/``
(:mod:`claude_task_runner.supervisor.account_pause`), never
``supervisor.json``, which a running supervisor rewrites from memory every
tick. The supervisor reads the markers every tick, before it dispatches, so
a change reaches a running supervisor within one tick and a stopped one
when it starts.
"""

from __future__ import annotations

import json as _json
from pathlib import Path

import typer
from rich.console import Console

from claude_task_runner.cli._helpers import (
    CWD_DEFAULT_LABEL,
    require_queue_option,
    resolve_per_queue_config,
)
from claude_task_runner.config.loader import load_settings, resolve_accounts
from claude_task_runner.config.schema import ResolvedAccount, Settings
from claude_task_runner.runner.account_dispatch import account_cap, account_in_flight_count
from claude_task_runner.supervisor import account_pause
from claude_task_runner.supervisor import persistence as persist_mod
from claude_task_runner.supervisor.states import (
    AccountState,
    SupervisorSnapshot,
)

app = typer.Typer(no_args_is_help=True, rich_markup_mode=None)


def _snapshot(settings: Settings, queue_dir: Path) -> SupervisorSnapshot | None:
    """Load supervisor.json or return None when it doesn't exist."""
    path = persist_mod.supervisor_state_path(queue_dir, settings.supervisor.state_file)
    return persist_mod.load(path)


def _account_row(
    acct: ResolvedAccount,
    snapshot: SupervisorSnapshot | None,
    paused: frozenset[str],
) -> dict[str, object]:
    """One row in ``account list``: decl + policy + observed state.

    ``paused`` is the set of names with a pause marker. It is read
    directly rather than from ``snapshot``, whose copy of the flag lags
    a pause or resume until the supervisor's next tick.
    """
    state: AccountState | None = None
    in_flight_count = 0
    if snapshot is not None:
        state = snapshot.accounts.get(acct.name)
        in_flight_count = account_in_flight_count(acct.name, snapshot.in_flight)
    policy = acct.policy
    dp = policy.dispatch_pct
    return {
        "name": acct.name,
        "config_dir": acct.config_dir,
        "linux_user": acct.linux_user,
        "max_concurrency": policy.concurrency.max_concurrency,
        "dispatch_pct": {
            "day": {
                "fivehr_slowdown_pct": dp.day.fivehr_slowdown_pct,
                "fivehr_stop_pct": dp.day.fivehr_stop_pct,
            },
            "night": {
                "fivehr_slowdown_pct": dp.night.fivehr_slowdown_pct,
                "fivehr_stop_pct": dp.night.fivehr_stop_pct,
                "time_start": dp.night.time_start,
                "time_end": dp.night.time_end,
            },
            "week": {
                "early_pct": dp.week.early_pct,
                "eow_pct": dp.week.eow_pct,
                "eow_time_switch": dp.week.eow_time_switch,
            },
            "timezone": dp.timezone,
        },
        "state": state.state.value if state is not None else None,
        "paused": acct.name in paused,
        "last_5h_util_pct": state.last_5h_util_pct if state is not None else None,
        "last_weekly_util_pct": state.last_weekly_util_pct if state is not None else None,
        "in_flight_count": in_flight_count,
        # The last throttle decision's cap (ADR-0022 ramp while
        # SLOWING_DOWN), and the number dispatch actually allows:
        # max_concurrency lowered to that target, or 0 for an account
        # that takes no tasks, including one with no state yet.
        "target_concurrency": state.target_concurrency if state is not None else None,
        "dispatch_cap": account_cap(acct, state) if state is not None else 0,
        "last_reading_at": state.last_reading_at if state is not None else None,
    }


@app.command("list")
def list_accounts(
    *,
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Per-queue claude_runner.toml."
    ),
    queue_dir: Path = typer.Option(
        Path.cwd, "--queue", help="Queue directory.", show_default=CWD_DEFAULT_LABEL
    ),
    json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """List configured accounts, their resolved policy and state.

    Skills use ``--json`` for machine-parseable output; operators run
    interactively without it for a human-readable summary.
    """
    console = Console()
    qd = require_queue_option(queue_dir, console, json=json)
    settings = load_settings(resolve_per_queue_config(config, qd))
    accounts = resolve_accounts(settings)
    snapshot = _snapshot(settings, qd)
    paused = account_pause.paused_names(qd)

    rows = [_account_row(a, snapshot, paused) for a in accounts]

    if json:
        print(_json.dumps({"accounts": rows}, default=str, indent=2))
        return

    if not rows:
        console.print("[dim]No accounts configured.[/]")
        return
    # Re-derive the per-account display fields directly from the resolved
    # account + (optional) state. The dict-of-object rows are designed
    # for JSON emission; pulling values straight from the model objects
    # keeps the type-checker happy and avoids object-indexing dances.
    for acct, row in zip(accounts, rows, strict=True):
        state_obj = snapshot.accounts.get(acct.name) if snapshot is not None else None
        paused_tag = " [yellow](paused)[/]" if row["paused"] else ""
        state_str = state_obj.state.value if state_obj is not None else "—"
        util_5h_s = f"{state_obj.last_5h_util_pct}%" if state_obj is not None else "—"
        util_w_s = f"{state_obj.last_weekly_util_pct}%" if state_obj is not None else "—"
        console.print(f"[bold]{acct.name}[/]{paused_tag}")
        console.print(f"  config_dir:      {acct.config_dir or '(default ~/.claude)'}")
        if acct.linux_user:
            console.print(f"  linux_user:      {acct.linux_user}")
        console.print(f"  max_concurrency: {acct.policy.concurrency.max_concurrency}")
        dp = acct.policy.dispatch_pct
        day_s = f"day {dp.day.fivehr_slowdown_pct or '~'}/{dp.day.fivehr_stop_pct or '~'}"
        night_s = (
            f"night {dp.night.fivehr_slowdown_pct or '~'}/{dp.night.fivehr_stop_pct or '~'}"
            f" [{dp.night.time_start or '~'}-{dp.night.time_end or '~'}]"
        )
        week_s = (
            f"week early={dp.week.early_pct or '~'}/eow={dp.week.eow_pct or '~'}"
            f"@{dp.week.eow_time_switch or '~'}"
        )
        console.print(f"  dispatch_pct:    {day_s}, {night_s}, {week_s}")
        console.print(
            f"  state:           {state_str}   "
            f"5h={util_5h_s}   weekly={util_w_s}   "
            f"in_flight={row['in_flight_count']}/{row['dispatch_cap']}"
        )


def _update_paused(
    queue_dir: Path,
    settings: Settings,
    name: str,
    *,
    paused: bool,
) -> tuple[bool, str]:
    """Create (pause) or remove (resume) ``name``'s pause marker.

    Returns (changed, message). ``changed`` is False when the marker was
    already in the requested state (idempotent pause/resume).
    Raises ``typer.BadParameter`` when ``name`` isn't configured.
    """
    if not any(a.name == name for a in settings.accounts):
        raise typer.BadParameter(
            f"account {name!r} not in [[accounts]]; configured: "
            + ", ".join(a.name for a in settings.accounts)
        )
    if not account_pause.set_paused(queue_dir, name, paused=paused):
        return False, f"account {name!r} already paused={paused!r}"
    return True, f"account {name!r} paused={paused!r}"


@app.command("pause")
def pause_account(
    name: str = typer.Argument(..., help="Account name to pause."),
    *,
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Per-queue claude_runner.toml."
    ),
    queue_dir: Path = typer.Option(
        Path.cwd, "--queue", help="Queue directory.", show_default=CWD_DEFAULT_LABEL
    ),
    json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Skip ``name`` from dispatch until ``account resume <name>``.

    The dispatcher consults the paused flag on the next tick; in-flight
    tasks already running against the account are NOT killed (the
    operator can use ``queue states`` and SIGTERM if needed).
    """
    console = Console()
    qd = require_queue_option(queue_dir, console, json=json)
    settings = load_settings(resolve_per_queue_config(config, qd))
    changed, message = _update_paused(qd, settings, name, paused=True)
    if json:
        print(_json.dumps({"changed": changed, "message": message}))
        return
    console.print(("[green]" if changed else "[dim]") + message + "[/]")


@app.command("resume")
def resume_account(
    name: str = typer.Argument(..., help="Account name to resume."),
    *,
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Per-queue claude_runner.toml."
    ),
    queue_dir: Path = typer.Option(
        Path.cwd, "--queue", help="Queue directory.", show_default=CWD_DEFAULT_LABEL
    ),
    json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Reverse ``account pause <name>``; dispatch includes it again."""
    console = Console()
    qd = require_queue_option(queue_dir, console, json=json)
    settings = load_settings(resolve_per_queue_config(config, qd))
    changed, message = _update_paused(qd, settings, name, paused=False)
    if json:
        print(_json.dumps({"changed": changed, "message": message}))
        return
    console.print(("[green]" if changed else "[dim]") + message + "[/]")
