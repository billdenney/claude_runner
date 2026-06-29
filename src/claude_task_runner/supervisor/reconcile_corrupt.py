"""Corrupt-state-file quarantine (ADR-0028).

A ``TaskState`` YAML can be left **unparseable** on disk — truncated or
garbled by a crash / power-loss that interrupts a write, or by any
non-atomic write path (the current serializer is atomic via
``write_state_atomic``, but historical files and external corruption
both exist). Every recovery sweep — :mod:`supervisor.reconcile_silent`,
:mod:`supervisor.adoption`, :func:`supervisor.reconcile.reconcile_orphans`
— and the orchestrator's own load loop *silently skip* such a file
(``logger.warning("skipping unparseable state file ...")`` then
``continue``). The result is a **corrupt-state zombie**: the task is
never reconciled, never re-dispatched, never finished, and its slot/id
is wedged. ``doctor``'s ``state_yamls`` check flags them, but nothing
recovers them at runtime.

This module is the missing layer. It scans the state dir, and for each
file that fails to parse it moves the file into ``state/.corrupt/``
(atomic ``os.replace`` within the same filesystem). Because
:func:`queue.store.list_state_files` globs the state dir
*non-recursively*, a quarantined file disappears from every sweep.

With its state file gone, the task reverts to the **no-state == pending**
baseline (the orchestrator's ``_DISPATCHABLE_STATUSES`` includes
``pending``) and re-dispatches normally — EXCEPT when a terminal
``completed`` status can be salvaged from the readable head of the
corrupt file, in which case a minimal valid state preserving
``completed`` is written so finished work is not redone. (Only
``completed`` is salvaged: re-dispatching a ``failed`` /
``failed_circuit_breaker`` / parked task is harmless or desirable, but
re-running a finished extraction wastes a full task.)
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from claude_task_runner.queue.schema import TaskState
from claude_task_runner.queue.store import (
    QueueIOError,
    list_state_files,
    load_state,
    state_dir,
    write_state_atomic,
)

logger = logging.getLogger(__name__)

#: stop_reason stamped on a state we rebuild after quarantining its
#: corrupt predecessor (only happens for the ``completed`` salvage path).
CORRUPT_STOP_REASON = "corrupt_state_quarantined"

#: subdirectory of the state dir where corrupt files are parked for
#: forensics. Leading dot keeps it clear of the ``*.yaml`` glob in
#: :func:`queue.store.list_state_files`.
CORRUPT_DIRNAME = ".corrupt"

#: only this terminal status is preserved across quarantine; every other
#: value (or an unsalvageable head) reverts the task to pending.
_SALVAGE_STATUSES = frozenset({"completed"})

#: ``status:`` sits in the first handful of lines of a TaskState YAML.
_STATUS_RE = re.compile(r"^status:\s*([A-Za-z_]+)\s*$")
_HEAD_LINES = 25


@dataclass(frozen=True)
class CorruptQuarantineResult:
    """One quarantined corrupt state file."""

    task_id: str
    quarantined_path: str
    #: terminal status preserved in a rebuilt state, else ``None`` (the
    #: task was reverted to pending and will re-dispatch).
    preserved_status: str | None


def _salvage_status(raw: str) -> str | None:
    """Best-effort read of the ``status:`` line from a corrupt file's
    head. The corruption we see in the wild (a stale multi-line-string
    fragment wedged after a shorter rewrite) leaves the top-level scalar
    fields intact, so the real status is recoverable even when the file
    as a whole won't parse."""
    for line in raw.splitlines()[:_HEAD_LINES]:
        m = _STATUS_RE.match(line)
        if m is not None:
            return m.group(1)
    return None


def quarantine_corrupt_state_files(
    queue_dir: Path,
    *,
    now: datetime | None = None,
) -> list[CorruptQuarantineResult]:
    """Detect and quarantine unparseable ``TaskState`` YAMLs.

    Returns one :class:`CorruptQuarantineResult` per quarantined file
    (empty when the state dir is clean). Safe to call repeatedly: a
    parseable file is left untouched, and a quarantined file is gone from
    the next scan. Never raises on a single bad file — a quarantine that
    itself fails (e.g. a transient ``OSError``) is logged and skipped so
    one stuck file can't wedge the whole sweep.
    """
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    sdir = state_dir(queue_dir)
    corrupt_dir = sdir / CORRUPT_DIRNAME
    results: list[CorruptQuarantineResult] = []

    for sp in list_state_files(queue_dir):
        try:
            load_state(sp)
            continue  # parseable — healthy, leave it alone
        except QueueIOError as exc:
            # Transient read error (file vanished mid-walk, permissions).
            # NOT corruption — leave it for the next tick.
            logger.debug("corrupt-state scan: transient read error on %s: %s", sp, exc)
            continue
        except Exception as exc:  # QueueSchemaError + any other parse failure
            corruption = exc

        task_id = sp.stem
        try:
            raw = sp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            raw = ""
        salvaged = _salvage_status(raw)

        # Atomic move into the quarantine dir (same filesystem as the
        # state dir, so os.replace is atomic). A unique suffix avoids
        # clobbering an earlier quarantine of the same task_id.
        try:
            corrupt_dir.mkdir(parents=True, exist_ok=True)
            dest = corrupt_dir / f"{task_id}.{stamp}.yaml"
            n = 1
            while dest.exists():
                dest = corrupt_dir / f"{task_id}.{stamp}.{n}.yaml"
                n += 1
            os.replace(sp, dest)
        except OSError as exc:
            logger.error(
                "corrupt-state quarantine: FAILED to move %s (%s); leaving in place",
                sp,
                exc,
            )
            continue

        preserved: str | None = None
        if salvaged in _SALVAGE_STATUSES:
            # Rebuild a minimal valid state so a finished task is not
            # redone. Run history / session_id can't be trusted from a
            # corrupt file, so they are intentionally dropped.
            try:
                write_state_atomic(
                    TaskState(
                        task_id=task_id,
                        status="completed",
                        stop_reason=CORRUPT_STOP_REASON,
                    ),
                    sp,
                )
                preserved = salvaged
            except Exception as exc:
                logger.error(
                    "corrupt-state quarantine: salvaged status=%s for %s but "
                    "failed to rebuild state (%s); task reverts to pending",
                    salvaged,
                    task_id,
                    exc,
                )

        logger.warning(
            "corrupt-state quarantine: %s was unparseable (%s); moved to %s; %s",
            task_id,
            corruption,
            dest,
            (
                f"rebuilt minimal status={preserved}"
                if preserved
                else "task reverts to pending and will re-dispatch"
            ),
        )
        results.append(
            CorruptQuarantineResult(
                task_id=task_id,
                quarantined_path=str(dest),
                preserved_status=preserved,
            )
        )

    return results
