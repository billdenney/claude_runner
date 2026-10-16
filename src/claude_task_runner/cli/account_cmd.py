"""``claude-task-runner account`` — operator surface for multi-account dispatch.

Subcommands:

* ``account list``   — show resolved accounts (queue-side decl + per-
  account policy) plus current per-account state (5h util, weekly util,
  in-flight count, paused?), and the queue-wide ceiling those caps add up
  to (:class:`runner.account_dispatch.QueueCeiling`).
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
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from claude_task_runner.cli._helpers import (
    CWD_DEFAULT_LABEL,
    load_settings_or_exit,
    queue_ceiling_json,
    queue_ceiling_text,
    require_queue_option,
    resolve_per_queue_config,
)
from claude_task_runner.config.loader import (
    BrokenAccount,
    per_account_toml_shown,
    resolve_accounts,
)
from claude_task_runner.config.schema import AccountSettings, ResolvedAccount, Settings
from claude_task_runner.runner.account_dispatch import (
    QueueCeiling,
    account_cap,
    account_in_flight_count,
)
from claude_task_runner.supervisor import account_pause
from claude_task_runner.supervisor import persistence as persist_mod
from claude_task_runner.supervisor.states import (
    AccountState,
    SupervisorSnapshot,
    SupervisorState,
)
from claude_task_runner.throttle.decision import weekly_catch_up
from claude_task_runner.throttle.policy import ResolvedPolicy, resolve
from claude_task_runner.usage.drift import usage_error_kind

app = typer.Typer(no_args_is_help=True, rich_markup_mode=None)


def _snapshot(settings: Settings, queue_dir: Path) -> SupervisorSnapshot | None:
    """Load supervisor.json or return None when it doesn't exist."""
    path = persist_mod.supervisor_state_path(queue_dir, settings.supervisor.state_file)
    return persist_mod.load(path)


def _state_fields(
    name: str,
    snapshot: SupervisorSnapshot | None,
    paused: frozenset[str],
    cap: int,
    resolved: ResolvedPolicy | None = None,
) -> dict[str, object]:
    """The observed-state part of an ``account list`` row.

    ``paused`` is the set of names with a pause marker. It is read
    directly rather than from ``snapshot``, whose copy of the flag lags
    a pause or resume until the supervisor's next tick. ``cap`` is the
    number dispatch allows the account by its state; a paused account is
    allowed 0 whatever its state. ``resolved`` is the account's merged
    dispatch policy, which dates a weekly throttle's end; ``None`` for an
    account whose settings file does not load.
    """
    state: AccountState | None = None
    in_flight_count = 0
    if snapshot is not None:
        state = snapshot.accounts.get(name)
        in_flight_count = account_in_flight_count(name, snapshot.in_flight)
    catch_up: datetime | None = None
    if (
        state is not None
        and resolved is not None
        and state.state is SupervisorState.THROTTLED_WEEKLY
        and state.last_weekly_reset_at is not None
    ):
        catch_up = weekly_catch_up(
            resolved,
            observed_pct=float(state.last_weekly_util_pct),
            resets_at=state.last_weekly_reset_at,
        )
    usage_error: dict[str, str] | None = None
    if state is not None and state.state is SupervisorState.ERROR_DRIFT:
        message = state.last_drift_message
        usage_error = {"kind": usage_error_kind(message), "message": message}
    return {
        "state": state.state.value if state is not None else None,
        "paused": name in paused,
        "last_5h_util_pct": state.last_5h_util_pct if state is not None else None,
        "last_weekly_util_pct": state.last_weekly_util_pct if state is not None else None,
        "in_flight_count": in_flight_count,
        # The last throttle decision's cap (ADR-0022 ramp while
        # SLOWING_DOWN), and the number dispatch actually allows:
        # max_concurrency lowered to that target, or 0 for an account
        # that takes no tasks: paused, or one with no state yet.
        "target_concurrency": state.target_concurrency if state is not None else None,
        "dispatch_cap": 0 if name in paused else cap,
        "last_reading_at": state.last_reading_at if state is not None else None,
        # A weekly-throttled account's catch-up time: when the weekly
        # target curve rises to its last reading. Not the supervisor's
        # next re-check (scheduled_wakeup_at), which comes sooner.
        "weekly_catch_up_at": catch_up,
        # Why an ERROR_DRIFT account has no reading: ``auth`` (log in
        # again) or ``format`` (the usage page changed), with the message.
        "usage_error": usage_error,
    }


def _account_row(
    acct: ResolvedAccount,
    snapshot: SupervisorSnapshot | None,
    paused: frozenset[str],
    settings: Settings,
) -> dict[str, object]:
    """One row in ``account list``: decl + policy + observed state."""
    state = snapshot.accounts.get(acct.name) if snapshot is not None else None
    policy = acct.policy
    dp = policy.dispatch_pct
    return {
        "name": acct.name,
        "config_dir": acct.config_dir,
        "linux_user": acct.linux_user,
        "policy_error": None,
        "policy_file": per_account_toml_shown(acct.config_dir),
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
        **_state_fields(
            acct.name,
            snapshot,
            paused,
            account_cap(acct, state) if state is not None else 0,
            resolve(settings, policy, acct.name),
        ),
    }


def _broken_row(
    decl: AccountSettings,
    broken: BrokenAccount,
    snapshot: SupervisorSnapshot | None,
    paused: frozenset[str],
) -> dict[str, object]:
    """One row in ``account list`` for an account whose file does not load.

    It has no policy to show, and dispatch allows it no tasks.
    """
    return {
        "name": decl.name,
        "config_dir": decl.config_dir,
        "linux_user": decl.linux_user,
        "policy_error": broken.error,
        "policy_file": broken.path,
        "max_concurrency": None,
        "dispatch_pct": None,
        **_state_fields(decl.name, snapshot, paused, 0),
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

    An account whose ``runner-account.toml`` does not load is listed with
    the error in place of its policy (with --json, in its row's
    ``policy_error``), the others as usual, and the command then exits 1.

    When the command cannot list at all, it prints only the error; with
    --json that is ``{"ok": false, "error": ...}``. ``"ok": false`` is how
    to tell those apart from a listing, which has no ``ok`` key, since
    exit 1 can mean either.

    \b
    Exit codes:
      0  every account can take tasks
      1  an account cannot take tasks: its runner-account.toml does not
         load, or its linux_user cannot be reached (the listing names
         it); or the queue TOML could not be read for now ("ok": false),
         which may clear up by itself
      2  --queue is not an existing directory, or the account pause
         markers cannot be read ("ok": false)
     78  the queue TOML's content is wrong ("ok": false)
    """
    console = Console()
    qd = require_queue_option(queue_dir, console, json=json)
    settings = load_settings_or_exit(resolve_per_queue_config(config, qd), console, json=json)
    resolution = resolve_accounts(settings)
    snapshot = _snapshot(settings, qd)
    try:
        paused = account_pause.paused_names(qd)
    except OSError as exc:
        msg = f"cannot read the account pause markers in {account_pause.pause_dir(qd)}: {exc}"
        if json:
            print(_json.dumps({"ok": False, "error": msg}))
        else:
            console.print(
                msg, style="bold red", markup=False, emoji=False, highlight=False, soft_wrap=True
            )
        raise typer.Exit(code=2) from exc

    loaded = {acct.name: acct for acct in resolution.accounts}
    broken = {b.name: b for b in resolution.broken}
    rows = [
        _account_row(loaded[decl.name], snapshot, paused, settings)
        if decl.name in loaded
        else _broken_row(decl, broken[decl.name], snapshot, paused)
        for decl in settings.accounts
    ]
    ceiling = QueueCeiling.of(
        settings.concurrency.max_concurrency,
        resolution.policies,
        broken=(b.name for b in resolution.broken),
    )

    if json:
        print(
            _json.dumps(
                {"accounts": rows, "queue_ceiling": queue_ceiling_json(ceiling, settings)},
                default=str,
                indent=2,
            )
        )
    elif not rows:
        console.print("[dim]No accounts configured.[/]")
    else:
        for decl, row in zip(settings.accounts, rows, strict=True):
            _print_account(console, decl, loaded.get(decl.name), broken.get(decl.name), row)
        console.print(
            f"[bold]queue ceiling:[/] {escape(queue_ceiling_text(ceiling, settings))}",
            highlight=False,
            soft_wrap=True,
        )
    if resolution.broken:
        raise typer.Exit(code=1)


def _print_account(
    console: Console,
    decl: AccountSettings,
    acct: ResolvedAccount | None,
    broken: BrokenAccount | None,
    row: dict[str, object],
) -> None:
    """The human-readable lines of one ``account list`` row."""
    paused_tag = " [yellow](paused)[/]" if row["paused"] else ""
    state_str = row["state"] or "—"
    util_5h_s = f"{row['last_5h_util_pct']}%" if row["state"] is not None else "—"
    util_w_s = f"{row['last_weekly_util_pct']}%" if row["state"] is not None else "—"
    console.print(f"[bold]{decl.name}[/]{paused_tag}")
    console.print(f"  config_dir:      {decl.config_dir or '(default ~/.claude)'}")
    if decl.linux_user:
        console.print(f"  linux_user:      {decl.linux_user}")
    if broken is not None:
        console.print(
            f"  [bold red]policy error:[/]    {escape(broken.error)}",
            highlight=False,
            soft_wrap=True,
        )
    if acct is not None:
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
    Raises ``typer.BadParameter`` when ``name`` isn't configured, except
    that a resume still removes the marker of an account taken out of
    ``[[accounts]]``, which would otherwise pause it again if re-added.
    """
    if not any(a.name == name for a in settings.accounts):
        if not paused and name in account_pause.paused_names(queue_dir):
            account_pause.set_paused(queue_dir, name, paused=False)
            return True, f"removed the pause marker of {name!r}, which is not in [[accounts]]"
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

    Creates the account's pause marker. A running supervisor stops
    dispatching new tasks to the account within one tick, and a stopped
    one from its next start. Tasks already running on the account are NOT
    killed (use ``queue states`` and SIGTERM if needed), and
    ``queue force-dispatch`` still dispatches to a paused account.
    """
    console = Console()
    qd = require_queue_option(queue_dir, console, json=json)
    settings = load_settings_or_exit(resolve_per_queue_config(config, qd), console, json=json)
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
    settings = load_settings_or_exit(resolve_per_queue_config(config, qd), console, json=json)
    changed, message = _update_paused(qd, settings, name, paused=False)
    if json:
        print(_json.dumps({"changed": changed, "message": message}))
        return
    console.print(("[green]" if changed else "[dim]") + message + "[/]")
