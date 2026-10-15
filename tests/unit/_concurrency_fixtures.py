"""Shared helpers for the per-account concurrency tests (ADR-0035).

Queues whose accounts' caps come from real ``runner-account.toml`` files,
loaded the way the supervisor loads them; dispatch ticks whose workers stay
in flight until released; a short ``start_daemon`` run; and the CLI.
"""

from __future__ import annotations

import os
import signal
import threading
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

from typer.testing import CliRunner

from claude_task_runner.cli import app
from claude_task_runner.clock import RealClock
from claude_task_runner.config.loader import PER_ACCOUNT_TOML_NAME, load_settings
from claude_task_runner.config.schema import (
    AccountConcurrencyPolicy,
    AccountPolicy,
    ResolvedAccount,
    Settings,
)
from claude_task_runner.queue.schema import Task
from claude_task_runner.queue.store import queue_runtime_dir, task_path_for, write_task_atomic
from claude_task_runner.runner import orchestrator as orch_mod
from claude_task_runner.runner.in_flight import DispatchSlot
from claude_task_runner.supervisor.daemon import start_daemon
from claude_task_runner.supervisor.states import (
    AccountState,
    SupervisorSnapshot,
    SupervisorState,
)
from claude_task_runner.usage.models import UsageReading, WindowReading
from claude_task_runner.usage.source import FakeUsageSource

DAEMON_LOGGER = "claude_task_runner.supervisor.daemon"


def write_account_cap(config_dir: Path, cap: int) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / PER_ACCOUNT_TOML_NAME).write_text(
        f"[concurrency]\nmax_concurrency = {cap}\n", encoding="utf-8"
    )


def queue_toml(path: Path, accounts: dict[str, Path], concurrency: str = "") -> Settings:
    """Write and load a ``claude_runner.toml`` declaring ``accounts``.

    ``concurrency`` is the body of a ``[concurrency]`` table; empty leaves the
    table out, as an operator who relies on the per-account caps would.
    """
    lines: list[str] = []
    for name, config_dir in accounts.items():
        lines += ["[[accounts]]", f'name = "{name}"', f'config_dir = "{config_dir}"', ""]
    if concurrency:
        lines += ["[concurrency]", concurrency, ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return load_settings(path)


def resolved_account(name: str, cap: int) -> ResolvedAccount:
    return ResolvedAccount(
        name=name,
        config_dir="",
        policy=AccountPolicy(concurrency=AccountConcurrencyPolicy(max_concurrency=cap)),
    )


def make_tasks(queue_dir: Path, n: int) -> None:
    for i in range(n):
        task = Task.model_validate({"id": f"t{i:02d}", "title": f"Task {i}", "prompt": "do it"})
        write_task_atomic(task, task_path_for(queue_dir, task.id))


def dispatching_snapshot(names: list[str]) -> SupervisorSnapshot:
    since = datetime(2026, 10, 15, tzinfo=UTC)
    return SupervisorSnapshot(
        state=SupervisorState.DISPATCHING,
        since=since,
        accounts={
            name: AccountState(state=SupervisorState.DISPATCHING, since=since) for name in names
        },
    )


class HeldDispatches:
    """Patches the dispatcher so every dispatched task stays in flight until
    :meth:`release`, the way a real worker does for minutes."""

    def __init__(self) -> None:
        self._gate = threading.Event()
        self._patch = patch(
            "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
            side_effect=lambda **_kw: self._gate.wait(timeout=30),
        )

    def __enter__(self) -> HeldDispatches:
        self._patch.start()
        return self

    def release(self, slots: dict[str, DispatchSlot]) -> None:
        self._gate.set()
        for slot in slots.values():
            slot.thread.join(timeout=5)

    def __exit__(self, *_exc: object) -> None:
        self._gate.set()
        self._patch.stop()


def busy_slots(stop: threading.Event, **by_account: int) -> dict[str, DispatchSlot]:
    """In-flight slots, ``n`` per account, whose threads run until ``stop``:
    tasks an earlier tick dispatched."""
    slots: dict[str, DispatchSlot] = {}
    for account, n in by_account.items():
        for i in range(n):
            thread = threading.Thread(target=stop.wait, kwargs={"timeout": 30}, daemon=True)
            thread.start()
            slots[f"busy-{account}-{i}"] = DispatchSlot(
                task_id=f"busy-{account}-{i}",
                account=account,
                started_at=datetime(2026, 10, 15, tzinfo=UTC),
                thread=thread,
            )
    return slots


def dispatch_tick(queue_dir: Path, settings: Settings, slots: dict[str, DispatchSlot]) -> None:
    """One dispatch pass. ``accounts`` is left to tick_dispatch, which
    resolves them from the files, as it does under the supervisor."""
    orch_mod.tick_dispatch(
        queue_dir=queue_dir,
        settings=settings,
        clock=RealClock(),
        snapshot=dispatching_snapshot([a.name for a in settings.accounts]),
        in_flight_slots=slots,
    )


def in_flight_by_account(slots: dict[str, DispatchSlot]) -> dict[str, int]:
    return dict(Counter(slot.account for slot in slots.values()))


def dispatched_by_account(slots: dict[str, DispatchSlot]) -> dict[str, int]:
    """Per account, the slots this test's ticks dispatched (not ``busy-*``)."""
    return dict(Counter(slot.account for tid, slot in slots.items() if not tid.startswith("busy-")))


def usage_reading() -> UsageReading:
    return UsageReading(
        captured_at=datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC),
        five_hour=WindowReading(
            utilization_pct=10,
            resets_at_raw="x",
            resets_at=datetime(2026, 10, 15, 17, 0, 0, tzinfo=UTC),
        ),
        seven_day=WindowReading(
            utilization_pct=10,
            resets_at_raw="x",
            resets_at=datetime(2026, 10, 20, 11, 0, 0, tzinfo=UTC),
        ),
    )


def single_account_body(tmp_path: Path, concurrency: str) -> str:
    body = f'[claude]\nconfig_dir = "{tmp_path / "acct"}"\n[usage]\npoll_interval_s = 0.01\n'
    if concurrency:
        body += f"[concurrency]\n{concurrency}\n"
    return body


def single_account_queue(tmp_path: Path, concurrency: str) -> tuple[Path, Path]:
    """A one-account queue whose account is capped at 3. Returns
    ``(queue_dir, config_path)``."""
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    write_account_cap(tmp_path / "acct", 3)
    config = tmp_path / "claude_runner.toml"
    config.write_text(single_account_body(tmp_path, concurrency), encoding="utf-8")
    return qd, config


def run_daemon(
    qd: Path,
    config: Path,
    *,
    pending_count_fn: Callable[[], int],
    install_signal_handlers: bool,
    max_ticks: int = 4,
) -> None:
    start_daemon(
        queue_dir=qd,
        settings=load_settings(config),
        source=FakeUsageSource([usage_reading()]),
        pending_count_fn=pending_count_fn,
        in_flight_count_fn=lambda: 0,
        install_signal_handlers=install_signal_handlers,
        max_ticks=max_ticks,
        config_path=config,
    )


def sighup_in_second_tick(before: Callable[[], None]) -> Callable[[], int]:
    """A ``pending_count_fn`` that runs ``before`` and sends SIGHUP during the
    second tick. It is called once at startup and once per tick, after that
    tick's ceiling check, so the reload runs at the start of the third."""
    calls = {"n": 0}

    def pending_count() -> int:
        calls["n"] += 1
        if calls["n"] == 3:
            before()
            os.kill(os.getpid(), signal.SIGHUP)
        return 0

    return pending_count


def invoke_cli(argv: list[str], columns: int = 1000) -> Any:
    return CliRunner().invoke(app, argv, env={"COLUMNS": str(columns)})
