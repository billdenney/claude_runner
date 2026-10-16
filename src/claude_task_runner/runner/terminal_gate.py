"""Auto-gate a task that closes on a terminal disposition (ADR-0033).

A run that ends as a clean SKIP or DEFER produces a deliverable report, no
commit, and a clean worktree. ``_finalize_state`` marks it ``completed``,
which is not in ``_DISPATCHABLE_STATUSES``, so on its own it never re-fires.

The leak is the sidecar-resume path. A task that filed a sidecar sits in
``awaiting_sidecar``; the moment every request has a response the
orchestrator makes it eligible again — deliberately, because that is how a
task collects an operator ruling and acts on it. But when the disposition
was terminal there is nothing to act on, so the task re-derives the same
skip at full effort and files the same sidecar again.

Observed on the nlmixr2lib queue: ``oare_PMC6930853`` (Schoemaker 2019) was
acked as a skip on 2026-09-02 and re-fired 24h later at ``effort: high``,
reaching the identical verdict; ``oare_PMC9823018`` did the same. The
selector reads only the ``block_dispatch`` register, so the fix that
actually holds is a row in that register — and until now nothing wrote one
except an operator by hand, which is a rule that lives only in prose.

This module writes that row from what the run already proved: a terminal
close with a deliverable on disk. It is deliberately conservative —

* it fires ONLY on the unambiguous clean-close shape (deliverable present,
  no commit, nothing uncommitted, nothing unpushed);
* it does not fire while the task has an open sidecar request with a
  question still outstanding (see :func:`unanswered_asks`). Such a run
  stopped to ask, not to close: answering makes it eligible again, which is
  how the answer reaches it, and a row would hold it down after the answer.
  A zero-question notification gets the row, since acknowledging one would
  otherwise re-dispatch a task that has nothing left to do;
* it never overwrites or rewrites an existing row, so an operator ruling
  already in the register wins;
* it writes ``status: AUTO_GATED`` and records the stop_reason and
  deliverable, so a row it wrote is distinguishable from a curated one and
  can be audited or reversed in bulk;
* any write failure is swallowed — the gate is an optimisation, and a
  register problem must never fail a run that genuinely succeeded.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import structlog

from claude_task_runner.queue.sidecar import OpenSidecar, open_sidecars

logger = structlog.get_logger(__name__)

#: Marks a row this module wrote, so auto-gates can be told from curated
#: operator rulings and reversed in bulk if the heuristic is ever wrong.
AUTO_GATE_STATUS = "AUTO_GATED"


def already_gated(queue_dir: Path, block_file: str | None, task_id: str) -> bool:
    """True when ``task_id`` already has a ``block_dispatch: true`` row.

    Mirrors the orchestrator's reader: every line is parsed independently
    and a line that is blank, malformed, or not task-keyed contributes
    nothing. An unreadable file reads as "not gated", which is the safe
    direction here — the worst case is a duplicate row, not a lost gate.
    """
    if not block_file:
        return False
    try:
        text = (queue_dir / block_file).read_text(encoding="utf-8")
    except OSError:
        return False
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
        except ValueError:
            continue
        if (
            isinstance(entry, dict)
            and entry.get("block_dispatch") is True
            and entry.get("task") == task_id
        ):
            return True
    return False


def unanswered_asks(queue_dir: Path, task_id: str) -> list[OpenSidecar]:
    """``task_id``'s open sidecar requests that ask the operator something.

    A request asks when it has at least one question its response has not
    answered (openness is per question, ADR-0031), or when the request or
    its response cannot be read well enough to tell (``error`` is set). The
    second stays open until an operator repairs it, and its answer must then
    be able to reach the task, so it counts as asking.

    A request with no questions is taken to be a notification and does not
    count, whatever other keys it carries. The v2 request schema has no
    notification marker, so this rests on asks being filed as ``questions``,
    as the agent-stop-and-ask skill specifies; ADR-0033's 2026-10-15
    amendment records the queue evidence behind that assumption.
    """
    return [
        item
        for item in open_sidecars(queue_dir, task_id=task_id)
        if item.outstanding or item.error is not None
    ]


def _describe_ask(item: OpenSidecar) -> str:
    """One request from :func:`unanswered_asks`, for a log line."""
    if item.error is not None:
        return f"request-{item.sequence:03d} unreadable ({item.error})"
    return f"request-{item.sequence:03d} asks {', '.join(item.outstanding)}"


def ensure_terminal_gate(
    *,
    queue_dir: Path,
    block_file: str | None,
    task_id: str,
    stop_reason: str | None,
    deliverable: str | None,
) -> bool:
    """Append a ``block_dispatch`` row for a terminal close. Returns True if written.

    No-ops when the feature is off (``block_file`` unset), when the run left
    questions for the operator (:func:`unanswered_asks`), or when a row
    already exists. The append is a single ``open(..., "a")`` write of one
    line, which is atomic enough for a JSONL register: concurrent appends
    interleave by line, never mid-line.
    """
    if not block_file or not task_id:
        return False
    asks = unanswered_asks(queue_dir, task_id)
    if asks:
        logger.info(
            "not auto-gating task %s: its open sidecar still asks the operator: %s",
            task_id,
            "; ".join(_describe_ask(item) for item in asks),
        )
        return False
    if already_gated(queue_dir, block_file, task_id):
        return False

    row = {
        "task": task_id,
        "block_dispatch": True,
        "needed": (
            "nothing - dispatch blocker only, written automatically when the run "
            "closed on a terminal disposition (ADR-0033)."
        ),
        "reason": (
            f"AUTO-GATED on a terminal close: the run produced a deliverable and no "
            f"commit, and left a clean worktree -- the shape of a skip or defer. "
            f"Without this row, answering the task's sidecar would make it eligible "
            f"again and it would re-derive the same disposition at full effort. "
            f"stop_reason={stop_reason or 'unknown'}. "
            f"Review the deliverable before treating this as a settled ruling; "
            f"replace this row with a curated one to record the real disposition."
        ),
        "deliverable": deliverable,
        "signals": {
            "blocker_type": "dispatch_blocker",
            "task": task_id,
            "severity": "closed",
            "blocks_dispatch": True,
            "auto_gated": True,
        },
        "status": AUTO_GATE_STATUS,
        "file_kind": "dispatch_blocker",
        "added": datetime.now(UTC).date().isoformat(),
        "detected_at": datetime.now(UTC).isoformat(),
    }
    try:
        with (queue_dir / block_file).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError as exc:
        # Never fail a run that succeeded because the register could not be
        # appended to; the task simply stays ungated as it was before.
        logger.warning(
            "could not auto-gate terminal close for %s (%s); task stays ungated",
            task_id,
            exc,
        )
        return False
    logger.info("auto-gated terminal close for task %s (stop_reason=%s)", task_id, stop_reason)
    return True
