"""Atomic JSON persistence for :class:`SupervisorSnapshot`.

Mirror of :mod:`queue.store`'s atomic-write pattern: tempfile +
``os.replace`` so a concurrent reader (the watchdog) always sees a
complete file.

Stored at ``<queue>/.claude_task_runner/supervisor.json`` per
``[supervisor].state_file``.

Handles two one-way migrations at load time:

* v2 → v3: the legacy single-account top-level fields wrap into
  ``accounts["default"]`` and un-attributed ``in_flight_task_ids``
  become attributed ``InFlightRecord`` entries.
* v3 → v4 (ADR-0022): any persisted ``state="paused_weekly"`` or
  ``state="end_of_week_push"`` rewrites to ``"idle"`` (both top-level
  and inside every ``accounts[*]``) and ``scheduled_wakeup_at``
  clears so the next tick recomputes wakeups against the new
  trace-following curve. In-flight tasks survive verbatim.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from claude_task_runner.queue.store import queue_runtime_dir
from claude_task_runner.supervisor.states import (
    SUPERVISOR_SCHEMA_VERSION,
    AccountState,
    SupervisorSnapshot,
    SupervisorState,
)


class SupervisorPersistenceError(ValueError):
    """Reading or writing ``supervisor.json`` failed."""


def supervisor_state_path(queue_dir: Path, state_file: str = "supervisor.json") -> Path:
    """Resolve ``<queue>/.claude_task_runner/<state_file>``."""
    return queue_runtime_dir(queue_dir) / state_file


_LEGACY_FIELDS_FOR_ACCOUNT_STATE = (
    "state",
    "since",
    "last_5h_util_pct",
    "last_weekly_util_pct",
    "last_5h_reset_at",
    "last_weekly_reset_at",
    "scheduled_wakeup_at",
    "consecutive_clean_polls",
    "last_drift_message",
)


def _migrate_v2_to_v3(
    payload: dict[str, Any], default_account_name: str = "default"
) -> dict[str, Any]:
    """Upgrade a v2 supervisor.json payload to v3 semantics.

    v2's top-level fields collapse into a single
    ``accounts[<default_account_name>]`` entry; legacy task ids in
    ``in_flight_task_ids`` are mapped to ``InFlightRecord`` objects
    whose ``started_at`` defaults to the snapshot's ``since`` (no
    per-task started_at was recorded in v2). One-way migration.
    """
    migrated = dict(payload)
    migrated["schema_version"] = 3

    acct_payload: dict[str, Any] = {
        key: migrated[key]
        for key in _LEGACY_FIELDS_FOR_ACCOUNT_STATE
        if key in migrated and migrated[key] is not None
    }
    if "state" not in acct_payload:
        acct_payload["state"] = SupervisorState.IDLE.value
    if "since" not in acct_payload:
        acct_payload["since"] = datetime(2026, 1, 1).isoformat()
    migrated["accounts"] = {default_account_name: acct_payload}

    legacy_in_flight = migrated.get("in_flight_task_ids") or []
    started_at = acct_payload["since"]
    migrated["in_flight"] = [
        {"task_id": tid, "account": default_account_name, "started_at": started_at}
        for tid in legacy_in_flight
    ]
    return migrated


_DROPPED_STATES_V4 = frozenset({"paused_weekly", "end_of_week_push"})
"""States removed by ADR-0022. Any persisted snapshot carrying one
is rewritten to ``idle`` so the next tick reclassifies under the new
trace-following rule."""


def _migrate_v3_to_v4(payload: dict[str, Any]) -> dict[str, Any]:
    """Upgrade a v3 supervisor.json payload to v4 semantics (ADR-0022).

    Rewrites any ``state == "paused_weekly" | "end_of_week_push"`` to
    ``"idle"`` — both at the top level and inside every
    ``accounts[*]`` entry — and clears ``scheduled_wakeup_at`` (both
    top-level and per-account) so the next tick recomputes wakeups
    against the new curve. In-flight task records are preserved
    verbatim: state migrations never kill running tasks.
    """
    migrated = dict(payload)
    migrated["schema_version"] = 4

    top_state = migrated.get("state")
    if isinstance(top_state, str) and top_state in _DROPPED_STATES_V4:
        migrated["state"] = SupervisorState.IDLE.value
    migrated["scheduled_wakeup_at"] = None

    accounts = migrated.get("accounts")
    if isinstance(accounts, dict):
        new_accounts: dict[str, Any] = {}
        for name, acct in accounts.items():
            if not isinstance(acct, dict):
                new_accounts[name] = acct
                continue
            acct_copy = dict(acct)
            acct_state = acct_copy.get("state")
            if isinstance(acct_state, str) and acct_state in _DROPPED_STATES_V4:
                acct_copy["state"] = SupervisorState.IDLE.value
            acct_copy["scheduled_wakeup_at"] = None
            new_accounts[name] = acct_copy
        migrated["accounts"] = new_accounts
    return migrated


def load(path: Path) -> SupervisorSnapshot | None:
    """Read a persisted snapshot, or ``None`` if the file doesn't exist.

    Raises :class:`SupervisorPersistenceError` if the file exists but
    can't be parsed — the daemon treats that as "fail loudly" rather
    than silently overwriting potentially-recoverable state.

    Performs a one-way v2 → v3 migration when an older file is
    encountered: the single-account top-level fields are folded into
    ``accounts["default"]``, and the un-attributed
    ``in_flight_task_ids`` becomes attributed ``in_flight`` records
    with ``account="default"``.
    """
    if not path.exists():
        return None
    try:
        with path.open("rb") as fh:
            payload = json.load(fh)
    except OSError as exc:
        raise SupervisorPersistenceError(f"failed to read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SupervisorPersistenceError(f"{path}: invalid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise SupervisorPersistenceError(f"{path}: top-level JSON must be an object")

    sv = payload.get("schema_version", SUPERVISOR_SCHEMA_VERSION)
    if sv == 2:
        payload = _migrate_v2_to_v3(payload)
        sv = 3
    if sv == 3:
        payload = _migrate_v3_to_v4(payload)
        sv = 4
    if sv != SUPERVISOR_SCHEMA_VERSION:
        raise SupervisorPersistenceError(
            f"{path}: schema_version={sv} does not match supported {SUPERVISOR_SCHEMA_VERSION}"
        )

    try:
        return SupervisorSnapshot.model_validate(payload)
    except ValidationError as exc:
        raise SupervisorPersistenceError(f"{path}: {exc}") from exc


def write_atomic(snapshot: SupervisorSnapshot, path: Path) -> None:
    """Atomic JSON write of the supervisor snapshot."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    payload = snapshot.model_dump(mode="json")
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=parent,
        delete=False,
        prefix=f".{path.name}.",
        suffix=".tmp",
    ) as tmp:
        json.dump(payload, tmp, indent=2, sort_keys=True, default=str)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def initial_snapshot(
    *,
    since: datetime,
    account_names: list[str] | None = None,
) -> SupervisorSnapshot:
    """Build a fresh snapshot for first-time supervisor start.

    Begins in ``IDLE`` so the next clean reading drives the first real
    classification. ``account_names`` (when provided) seeds the
    per-account state map with one entry per account; each account
    starts in IDLE. ``None`` (the legacy default) produces a snapshot
    with a single ``"default"`` entry, matching the v2 single-account
    flow.
    """
    names = account_names if account_names is not None else ["default"]
    accounts = {name: AccountState(state=SupervisorState.IDLE, since=since) for name in names}
    return SupervisorSnapshot(
        state=SupervisorState.IDLE,
        since=since,
        accounts=accounts,
    )
