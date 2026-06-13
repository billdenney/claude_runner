"""Operator override for "dispatch this task NOW, ignore the throttle."

The supervisor's normal dispatch path is gated by the 5h / weekly
throttle bands (:func:`supervisor.state_machine.step`) and the
priority sort in :func:`runner.orchestrator.tick_dispatch`. Force-
dispatch bypasses both: a single task identified by ``task_id`` is
dispatched on the next supervisor tick (or synchronously, when the
supervisor is not running) regardless of the supervisor's current
state vertex.

Two-phase protocol so the CLI is safe to invoke against a live
supervisor (no race with the supervisor's own dispatch loop):

1. The CLI calls :func:`write_request`, dropping a JSON file at
   ``<queue>/.claude_task_runner/force_dispatch/<task_id>.req``.
2. The supervisor's tick loop calls :func:`tick_consume` BEFORE the
   throttle gate. It scans the request directory, revalidates each
   task (still in ``todo/``, still in a dispatchable status, not
   already in-flight), respects ``max_concurrency`` unless the
   request was written with ``allow_over_limit=True``, and spawns
   exactly the same per-task dispatch thread the orchestrator does.

For the "no supervisor running" case (CLI smoke tests, local
fixture-queue debugging), :func:`dispatch_synchronously` runs one
attempt in-process. There is no race because the host-wide lock
isn't held by anyone else.

Request files persist across ticks when ``max_concurrency`` is full
and ``allow_over_limit=False``: the supervisor declines, leaves the
file in place, and tries again on the next tick. Operators wanting
the request to evaporate on capacity-decline must remove it manually.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from claude_task_runner.config.loader import resolve_accounts
from claude_task_runner.queue.schema import Task, TaskState
from claude_task_runner.queue.store import (
    load_state,
    load_task,
    queue_runtime_dir,
    state_path_for,
    task_path_for,
)
from claude_task_runner.runner import dispatcher as dispatcher_mod
from claude_task_runner.runner.in_flight import DispatchSlot
from claude_task_runner.runner.session import plan_next_spawn

if TYPE_CHECKING:
    from claude_task_runner.clock import Clock
    from claude_task_runner.config.schema import Settings

logger = logging.getLogger(__name__)


class ForceDispatchError(RuntimeError):
    """Raised when a force-dispatch precondition fails.

    Reasons: task YAML missing from ``todo/``, task already in a
    non-dispatchable status (``running``, ``awaiting_sidecar``,
    ``completed``, ``failed_circuit_breaker``), or, in the
    synchronous path, ``claude`` is not on PATH.
    """


@dataclass(frozen=True)
class ForceDispatchRequest:
    """Parsed representation of a ``.req`` file under ``force_dispatch/``."""

    task_id: str
    requested_at: datetime
    allow_over_limit: bool = False


# A task in any of these states (or with no state file at all) is
# eligible to be force-dispatched. Anything else means the supervisor
# (or the dispatcher) is already operating on the task, and we refuse
# to overlap.
_FORCE_DISPATCHABLE = frozenset({None, "pending", "failed"})


def force_dispatch_dir(queue_dir: Path) -> Path:
    """Resolve ``<queue>/.claude_task_runner/force_dispatch/`` (no mkdir)."""
    return queue_dir / ".claude_task_runner" / "force_dispatch"


def request_path(queue_dir: Path, task_id: str) -> Path:
    """Conventional path of a force-dispatch request file for ``task_id``."""
    return force_dispatch_dir(queue_dir) / f"{task_id}.req"


def write_request(
    queue_dir: Path,
    task_id: str,
    *,
    allow_over_limit: bool = False,
    clock: Clock | None = None,
) -> Path:
    """Atomically write a force-dispatch request file for ``task_id``.

    Overwrites any existing request for the same task id. Creates
    parent directories as needed. Returns the file path. The supervisor
    consumes the request on its next tick (see :func:`tick_consume`).
    """
    from claude_task_runner.clock import RealClock

    now = (clock or RealClock()).now()
    fd_dir = force_dispatch_dir(queue_dir)
    fd_dir.mkdir(parents=True, exist_ok=True)
    target = request_path(queue_dir, task_id)
    payload = {
        "task_id": task_id,
        "requested_at": now.isoformat(),
        "allow_over_limit": bool(allow_over_limit),
    }
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=target.parent,
        delete=False,
        prefix=f".{target.name}.",
        suffix=".tmp",
    ) as tmp:
        json.dump(payload, tmp)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, target)
    return target


def _parse_request_file(path: Path) -> ForceDispatchRequest | None:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("force-dispatch: cannot parse request %s: %s", path, exc)
        return None
    try:
        return ForceDispatchRequest(
            task_id=str(data["task_id"]),
            requested_at=datetime.fromisoformat(str(data["requested_at"])),
            allow_over_limit=bool(data.get("allow_over_limit", False)),
        )
    except (KeyError, ValueError, TypeError) as exc:
        logger.warning("force-dispatch: malformed request %s: %s", path, exc)
        return None


def list_requests(queue_dir: Path) -> list[ForceDispatchRequest]:
    """Return parsed requests sorted oldest-first by ``requested_at``."""
    out: list[ForceDispatchRequest] = []
    fd_dir = force_dispatch_dir(queue_dir)
    if not fd_dir.exists():
        return out
    for path in sorted(fd_dir.glob("*.req")):
        req = _parse_request_file(path)
        if req is not None:
            out.append(req)
    out.sort(key=lambda r: r.requested_at)
    return out


def consume_request(queue_dir: Path, task_id: str) -> None:
    """Delete the request file for ``task_id``. Idempotent."""
    path = request_path(queue_dir, task_id)
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


def _load_state_or_none(queue_dir: Path, task_id: str) -> TaskState | None:
    """Load the task's state YAML, or ``None`` when it legitimately doesn't exist.

    A *missing* state file is normal (the task was never dispatched) and
    returns ``None``. A *corrupt* state file is NOT treated as "no prior
    state": silently swallowing a parse error here would let a completed
    or in-flight task be re-dispatched (the status check downstream reads
    ``None`` as dispatchable). Parse/read errors are logged and re-raised
    as :class:`ForceDispatchError` so the corruption surfaces to the
    operator instead of being papered over.
    """
    state_path = state_path_for(queue_dir, task_id)
    if not state_path.exists():
        # Legitimately no prior state — the task hasn't been dispatched.
        return None
    try:
        return load_state(state_path)
    except Exception as exc:
        # The file exists but could not be read/parsed (truncated write,
        # disk corruption, schema drift). ``load_state`` wraps a vanished
        # file as ``QueueIOError(FileNotFoundError)``; a genuine TOCTOU
        # disappearance is still "no prior state".
        if not state_path.exists():
            return None
        logger.error(
            "force-dispatch %s: state YAML %s is unreadable/corrupt (%s); "
            "refusing to treat as 'no prior state'",
            task_id,
            state_path,
            exc,
        )
        raise ForceDispatchError(
            f"task {task_id} state YAML is unreadable/corrupt: {state_path} ({exc})"
        ) from exc


def tick_consume(
    *,
    queue_dir: Path,
    settings: Settings,
    clock: Clock,
    in_flight_slots: dict[str, DispatchSlot],
    claude_executable: str = "claude",
) -> int:
    """Drain the force-dispatch request queue; return tasks actually dispatched.

    Invoked at the top of each supervisor tick, BEFORE the throttle gate
    in :func:`runner.orchestrator.tick_dispatch`. Requests whose task is
    no longer dispatchable (deleted, completed, in-flight elsewhere) are
    consumed and dropped. Requests that hit the concurrency cap are left
    in place for the next tick unless ``allow_over_limit`` was set.

    Force-dispatched tasks honour ``task.account`` pinning when set;
    otherwise they route to the first configured account that exists.
    """
    requests = list_requests(queue_dir)
    if not requests:
        return 0

    accounts = resolve_accounts(settings)
    accounts_by_name = {a.name: a for a in accounts}

    dispatched = 0
    cap = max(1, settings.concurrency.max_concurrency)

    for req in requests:
        task_path = task_path_for(queue_dir, req.task_id)
        if not task_path.exists():
            logger.warning(
                "force-dispatch %s: task YAML missing from todo/; dropping request",
                req.task_id,
            )
            consume_request(queue_dir, req.task_id)
            continue
        try:
            task = load_task(task_path)
        except Exception as exc:
            logger.warning(
                "force-dispatch %s: task YAML unparseable (%s); dropping request",
                req.task_id,
                exc,
            )
            consume_request(queue_dir, req.task_id)
            continue

        if task.id in in_flight_slots:
            logger.info(
                "force-dispatch %s: already in-flight; consuming request",
                task.id,
            )
            consume_request(queue_dir, task.id)
            continue

        try:
            state = _load_state_or_none(queue_dir, task.id)
        except ForceDispatchError as exc:
            # Corrupt state YAML: we cannot tell whether the task is
            # already running/completed, so we must NOT dispatch. Leave
            # the request in place (do not consume) so the operator can
            # repair the state and the task is retried next tick rather
            # than silently re-dispatched.
            logger.error(
                "force-dispatch %s: %s; leaving request for operator repair",
                task.id,
                exc,
            )
            continue
        status = state.status if state is not None else None
        if status not in _FORCE_DISPATCHABLE:
            logger.warning(
                "force-dispatch %s: status %s is not dispatchable; dropping request",
                task.id,
                status,
            )
            consume_request(queue_dir, task.id)
            continue

        if not req.allow_over_limit and len(in_flight_slots) >= cap:
            logger.warning(
                "force-dispatch %s: in-flight=%d >= max_concurrency=%d; leaving request",
                task.id,
                len(in_flight_slots),
                cap,
            )
            continue

        # Pick the account. Force-dispatch deliberately bypasses
        # choose_account's eligibility filter (that's the point of
        # "force"), so a paused or throttled account still gets the
        # task — the operator opted in. Account selection precedence:
        #
        #   1. Session affinity (ADR-0024). When the task has a
        #      session_id, it MUST resume on the host account —
        #      sessions are namespaced by CLAUDE_CONFIG_DIR, so
        #      dispatching to a different account produces a 0.85s
        #      ``No conversation found with session ID`` error.
        #      ``--over-limit`` is a throttle bypass; it is NOT a
        #      bypass of this correctness invariant. If the host
        #      account is no longer configured, drop the request and
        #      tell the operator to run ``queue restart-fresh`` so
        #      the next dispatch starts a fresh session.
        #   2. Task.account pinning when present and known.
        #   3. First configured account (fallback).
        affined = state.session_host_account() if state is not None else None
        if affined is not None:
            if affined not in accounts_by_name:
                logger.warning(
                    "force-dispatch %s: session affined to %r which is "
                    "not in [[accounts]]; dropping request. Run "
                    "`queue restart-fresh %s` to clear the session "
                    "and dispatch fresh.",
                    task.id,
                    affined,
                    task.id,
                )
                consume_request(queue_dir, task.id)
                continue
            picked = accounts_by_name[affined]
        elif task.account and task.account in accounts_by_name:
            picked = accounts_by_name[task.account]
        else:
            picked = next(iter(accounts_by_name.values()))

        consume_request(queue_dir, task.id)
        _spawn_dispatch_thread(
            task=task,
            queue_dir=queue_dir,
            settings=settings,
            clock=clock,
            claude_executable=claude_executable,
            in_flight_slots=in_flight_slots,
            claude_config_dir=picked.config_dir,
            linux_user=picked.linux_user,
            account=picked.name,
        )
        dispatched += 1

    return dispatched


def _spawn_dispatch_thread(
    *,
    task: Task,
    queue_dir: Path,
    settings: Settings,
    clock: Clock,
    claude_executable: str,
    in_flight_slots: dict[str, DispatchSlot],
    claude_config_dir: str,
    linux_user: str | None,
    account: str,
) -> None:
    """Spawn the same shape of dispatch thread the orchestrator does."""
    # Local import to avoid a circular dependency at module-load time:
    # orchestrator imports nothing from force_dispatch, but we import
    # the orchestrator's private dispatch worker to keep the spawn /
    # dispatch / reap shape identical.
    from claude_task_runner.runner.orchestrator import _dispatch_one_safely

    thread = threading.Thread(
        target=_dispatch_one_safely,
        args=(
            task,
            queue_dir,
            settings,
            clock,
            claude_executable,
            claude_config_dir,
            linux_user,
            account,
        ),
        name=f"force-dispatch-{task.id}",
        daemon=False,
    )
    thread.start()
    in_flight_slots[task.id] = DispatchSlot(
        task_id=task.id,
        account=account,
        started_at=clock.now(),
        thread=thread,
    )
    logger.info(
        "force-dispatched task %s via account=%s (in_flight=%d)",
        task.id,
        account,
        len(in_flight_slots),
    )


def dispatch_synchronously(
    *,
    task_id: str,
    queue_dir: Path,
    settings: Settings,
    clock: Clock,
    claude_executable: str = "claude",
) -> TaskState:
    """Run one dispatch attempt for ``task_id`` in-process; return final state.

    Used by the CLI when no supervisor is running. There is no race
    because the host-wide lock isn't held by anyone else, and we don't
    write a request file at all. The pre-dispatch hook runs (same as a
    normal dispatch). Throttle gates are skipped — that is the whole
    point of force-dispatch.
    """
    queue_runtime_dir(queue_dir)
    task_path = task_path_for(queue_dir, task_id)
    if not task_path.exists():
        raise ForceDispatchError(f"task YAML not in todo/: {task_path}")
    try:
        task = load_task(task_path)
    except Exception as exc:
        raise ForceDispatchError(f"cannot parse task {task_id}: {exc}") from exc

    state = _load_state_or_none(queue_dir, task.id) or TaskState(task_id=task.id)
    if state.status not in _FORCE_DISPATCHABLE:
        raise ForceDispatchError(f"task {task.id} status={state.status!r} is not dispatchable")

    accounts = resolve_accounts(settings)
    accounts_by_name = {a.name: a for a in accounts}
    # ADR-0024: session affinity wins over task.account pinning when
    # both are set. A session opened on account X is invisible to
    # claude running with account Y's CLAUDE_CONFIG_DIR, so this is
    # a correctness gate, not a policy choice.
    affined = state.session_host_account()
    if affined is not None:
        if affined not in accounts_by_name:
            raise ForceDispatchError(
                f"task {task.id} session affined to {affined!r} which "
                "is not in [[accounts]]; run `queue restart-fresh "
                f"{task.id}` to clear the session and dispatch fresh"
            )
        picked = accounts_by_name[affined]
    elif task.account and task.account in accounts_by_name:
        picked = accounts_by_name[task.account]
    else:
        picked = next(iter(accounts_by_name.values()))

    plan = plan_next_spawn(task, state, settings=settings.session)
    outcome = dispatcher_mod.dispatch(
        task=task,
        state=state,
        plan=plan,
        queue_dir=queue_dir,
        clock=clock,
        settings_caps=settings.task_caps,
        settings_session=settings.session,
        settings_hooks=settings.hooks,
        settings_failure_classifier=settings.failure_classifier,
        claude_executable=claude_executable,
        claude_config_dir=picked.config_dir,
        linux_user=picked.linux_user,
        account=picked.name,
    )
    return outcome.new_state
