"""``claude-task-runner account`` — operator surface for multi-account dispatch.

Subcommands:

* ``account list``   — show resolved accounts (queue-side decl + per-
  account policy) plus current per-account state (5h util, weekly util,
  in-flight count, paused?).
* ``account pause``  — set ``paused=true`` on an account's
  :class:`AccountState`; the dispatcher skips it until ``resume``.
* ``account resume`` — clear ``paused``.

Skills do not import schemas directly; they invoke
``account list --json`` and parse the output.

Pause / resume mutate ``supervisor.json``. When the supervisor is
running it picks up the change on its next tick (no signal needed —
the state file is the source of truth). When the supervisor is not
running the operator can still flip the flag and the next start
honours it.
"""

from __future__ import annotations

import json as _json
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console

from claude_task_runner.config.loader import load_settings, resolve_accounts
from claude_task_runner.config.schema import ResolvedAccount, Settings
from claude_task_runner.runner.account_dispatch import account_in_flight_count
from claude_task_runner.supervisor import persistence as persist_mod
from claude_task_runner.supervisor.states import (
    AccountState,
    SupervisorSnapshot,
    SupervisorState,
)

app = typer.Typer(no_args_is_help=True)


def _snapshot(settings: Settings, queue_dir: Path) -> SupervisorSnapshot | None:
    """Load supervisor.json or return None when it doesn't exist."""
    path = persist_mod.supervisor_state_path(queue_dir, settings.supervisor.state_file)
    return persist_mod.load(path)


def _account_row(
    acct: ResolvedAccount,
    snapshot: SupervisorSnapshot | None,
) -> dict[str, object]:
    """One row in ``account list``: decl + policy + observed state."""
    state: AccountState | None = None
    in_flight_count = 0
    if snapshot is not None:
        state = snapshot.accounts.get(acct.name)
        in_flight_count = account_in_flight_count(acct.name, snapshot.in_flight)
    policy = acct.policy
    return {
        "name": acct.name,
        "config_dir": acct.config_dir,
        "linux_user": acct.linux_user,
        "max_concurrency": policy.concurrency.max_concurrency,
        "throttle": {
            "five_hour": {
                "daytime_full": policy.throttle.five_hour.daytime_band_full_dispatch_max_pct,
                "daytime_slow": policy.throttle.five_hour.daytime_band_slowdown_max_pct,
                "nighttime_full": policy.throttle.five_hour.nighttime_band_full_dispatch_max_pct,
                "nighttime_slow": policy.throttle.five_hour.nighttime_band_slowdown_max_pct,
            },
            "day_end": policy.throttle.time_of_day.day_end,
        },
        "state": state.state.value if state is not None else None,
        "paused": state.paused if state is not None else False,
        "last_5h_util_pct": state.last_5h_util_pct if state is not None else None,
        "last_weekly_util_pct": state.last_weekly_util_pct if state is not None else None,
        "in_flight_count": in_flight_count,
    }


@app.command("list")
def list_accounts(
    *,
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Per-queue claude_runner.toml."
    ),
    queue_dir: Path = typer.Option(Path.cwd, "--queue", help="Queue directory."),
    json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """List configured accounts with their resolved policy and current state.

    Skills use ``--json`` for machine-parseable output; operators run
    interactively without it for a human-readable summary.
    """
    console = Console()
    settings = load_settings(config)
    accounts = resolve_accounts(settings)
    snapshot = _snapshot(settings, queue_dir.resolve())

    rows = [_account_row(a, snapshot) for a in accounts]

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
        paused = " [yellow](paused)[/]" if state_obj is not None and state_obj.paused else ""
        state_str = state_obj.state.value if state_obj is not None else "—"
        util_5h_s = f"{state_obj.last_5h_util_pct}%" if state_obj is not None else "—"
        util_w_s = f"{state_obj.last_weekly_util_pct}%" if state_obj is not None else "—"
        console.print(f"[bold]{acct.name}[/]{paused}")
        console.print(f"  config_dir:      {acct.config_dir or '(default ~/.claude)'}")
        if acct.linux_user:
            console.print(f"  linux_user:      {acct.linux_user}")
        console.print(f"  max_concurrency: {acct.policy.concurrency.max_concurrency}")
        f = acct.policy.throttle.five_hour
        console.print(
            f"  bands:           "
            f"daytime {f.daytime_band_full_dispatch_max_pct}/{f.daytime_band_slowdown_max_pct}, "
            f"nighttime {f.nighttime_band_full_dispatch_max_pct}/{f.nighttime_band_slowdown_max_pct}, "
            f"day_end={acct.policy.throttle.time_of_day.day_end}"
        )
        console.print(
            f"  state:           {state_str}   "
            f"5h={util_5h_s}   weekly={util_w_s}   in_flight={row['in_flight_count']}"
        )


def _update_paused(
    queue_dir: Path,
    settings: Settings,
    name: str,
    *,
    paused: bool,
) -> tuple[bool, str]:
    """Persist a paused/unpaused change for ``name``.

    Returns (changed, message). ``changed`` is False when the account
    state already had the requested value (idempotent pause/resume).
    Raises ``typer.BadParameter`` when ``name`` isn't configured.
    """
    if not any(a.name == name for a in settings.accounts):
        raise typer.BadParameter(
            f"account {name!r} not in [[accounts]]; configured: "
            + ", ".join(a.name for a in settings.accounts)
        )
    state_path = persist_mod.supervisor_state_path(queue_dir, settings.supervisor.state_file)
    snapshot = persist_mod.load(state_path)
    if snapshot is None:
        # No snapshot yet (supervisor never started here): seed one
        # with the requested account paused. Next start picks it up.
        snapshot = persist_mod.initial_snapshot(
            since=datetime.now(UTC),
            account_names=[a.name for a in settings.accounts],
        )
    current = snapshot.accounts.get(name)
    if current is None:
        # Account configured but no state row: synthesize an IDLE row.
        current = AccountState(state=SupervisorState.IDLE, since=datetime.now(UTC))
    if current.paused == paused:
        return False, f"account {name!r} already paused={paused!r}"
    new_state = current.model_copy(update={"paused": paused})
    new_accounts = {**snapshot.accounts, name: new_state}
    new_snapshot = snapshot.model_copy(update={"accounts": new_accounts})
    persist_mod.write_atomic(new_snapshot, state_path)
    return True, f"account {name!r} paused={paused!r}"


@app.command("pause")
def pause_account(
    name: str = typer.Argument(..., help="Account name to pause."),
    *,
    config: Path | None = typer.Option(
        None, "--config", "-c", help="Per-queue claude_runner.toml."
    ),
    queue_dir: Path = typer.Option(Path.cwd, "--queue", help="Queue directory."),
    json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Skip ``name`` from dispatch until ``account resume <name>``.

    The dispatcher consults the paused flag on the next tick; in-flight
    tasks already running against the account are NOT killed (the
    operator can use ``queue states`` and SIGTERM if needed).
    """
    console = Console()
    settings = load_settings(config)
    changed, message = _update_paused(queue_dir.resolve(), settings, name, paused=True)
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
    queue_dir: Path = typer.Option(Path.cwd, "--queue", help="Queue directory."),
    json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
) -> None:
    """Reverse ``account pause <name>``; the dispatcher includes it again."""
    console = Console()
    settings = load_settings(config)
    changed, message = _update_paused(queue_dir.resolve(), settings, name, paused=False)
    if json:
        print(_json.dumps({"changed": changed, "message": message}))
        return
    console.print(("[green]" if changed else "[dim]") + message + "[/]")
