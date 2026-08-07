"""Mechanical (no-AI, no-dispatch) task readiness gates. See ADR-0030.

The supervisor's candidate selector (:func:`runner.orchestrator
._eligible_candidates`) calls :func:`unmet_requirements` for every otherwise-
eligible task on each tick. Every check here is a cheap, pure, in-process
probe — a filesystem ``exists()`` or a set membership test — so it is safe to
run for the whole pending pool every tick. Nothing here spawns a worker, runs
a hook subprocess, writes state, or consults an LLM.

The gate binds on EVERY dispatch decision, not just the first. Three call
sites enforce it, and each exists because a task can reach a spawn without
passing the one before it:

* the selector, for every resume status — ``pending``, ``failed``,
  ``deferred`` past its cooldown, and ``awaiting_sidecar`` whose requests
  have all been answered (that last one re-enters the eligible set purely
  because a response file appeared, so it must be re-gated, not grandfathered
  in on the strength of having been dispatched before);
* :func:`runner.orchestrator._dispatch_one_safely`, the thread entrypoint
  every path funnels through, as the structural backstop;
* :mod:`runner.force_dispatch`, whose operator override is scoped to the
  throttle and not to a missing input.

When the selector holds a task it records the reason on the task's state as
a ``deferred`` park (see :data:`HOLD_REASON_PREFIX`), so a mechanical block is
visible in ``queue list`` rather than being an invisible skip.

This exists so a task *waiting on a file* behaves like a task *waiting on a
sidecar response* always has: parked purely by the selector, re-checked every
tick, and dispatched the instant the element appears — instead of being
dispatched (burning an in-flight slot and a dispatch cycle) only to have the
pre-dispatch hook exit-1 defer it, then re-checked no sooner than the next
``deferral_recheck_cooldown_s``.

Adding a new gate type is two edits: extend
:data:`claude_task_runner.queue.schema.ReadinessKind` and add a branch here.
Keep every branch cheap and side-effect-free — the scaling contract is "safe
to run for every task every tick," which a per-task subprocess would break.
"""

from __future__ import annotations

import logging
from pathlib import Path

from claude_task_runner.queue.schema import ReadinessRequirement, Task
from claude_task_runner.queue.sidecar import list_open_sidecars

logger = logging.getLogger(__name__)


def _resolve(queue_dir: Path, raw: str) -> Path:
    """Resolve a requirement path: absolute as-is, relative against the queue."""
    p = Path(raw)
    return p if p.is_absolute() else (queue_dir / p)


def _describe(req: ReadinessRequirement, reason: str) -> str:
    """One human-readable line for an unmet requirement (with the note, if any)."""
    return reason + (f" ({req.note})" if req.note else "")


def unmet_requirements(
    task: Task,
    queue_dir: Path,
    *,
    open_sidecar_task_ids: set[str] | None = None,
) -> list[str]:
    """Return descriptions of ``task``'s UNMET readiness requirements.

    Empty list ⇒ the task is mechanically ready to dispatch. Each returned
    string is a short operator-facing reason (e.g. ``"missing file: …"``).

    Pure and cheap: only ``Path.exists()`` probes and a set lookup, so this is
    safe to call for every candidate on every supervisor tick. It never
    dispatches, spawns a worker, writes state, or consults an LLM.

    ``open_sidecar_task_ids`` is the precomputed set of task ids with an
    unanswered sidecar (from :func:`queue.sidecar.list_open_sidecars`). Pass
    it so a per-tick sweep computes the set once and shares it across tasks;
    when ``None`` and a ``sidecar_response`` requirement is present, it is
    computed on demand.
    """
    unmet: list[str] = []
    for req in task.requires:
        if req.kind == "file":
            # ``path`` is guaranteed non-empty for kind="file" by
            # ReadinessRequirement's validator.
            assert req.path is not None
            target = _resolve(queue_dir, req.path)
            if not target.exists():
                unmet.append(_describe(req, f"missing file: {target}"))
        elif req.kind == "sidecar_response":
            if open_sidecar_task_ids is None:
                open_sidecar_task_ids = {tid for tid, _seq, _p in list_open_sidecars(queue_dir)}
            if task.id in open_sidecar_task_ids:
                unmet.append(_describe(req, "awaiting sidecar response"))
        else:  # pragma: no cover - unreachable while ReadinessKind is exhaustive
            # Fail closed: an unrecognised kind blocks dispatch (loudly) rather
            # than being silently treated as satisfied. Reaching here means the
            # schema Literal grew a kind this evaluator wasn't taught.
            logger.error("task %s: unknown readiness kind %r; treating as unmet", task.id, req.kind)
            unmet.append(_describe(req, f"unknown requirement kind: {req.kind}"))
    return unmet


def is_ready(
    task: Task,
    queue_dir: Path,
    *,
    open_sidecar_task_ids: set[str] | None = None,
) -> bool:
    """True iff ``task`` has no unmet readiness requirements."""
    return not unmet_requirements(task, queue_dir, open_sidecar_task_ids=open_sidecar_task_ids)


#: Marker every runner-written readiness hold reason starts with. It is what
#: distinguishes a hold this gate parked (and may therefore un-park on its own,
#: the moment the requirement is satisfied) from an operator's manual
#: ``deferred_reason`` or the pre-dispatch hook's exit-1 deferral — neither of
#: which the runner may silently overwrite or clear.
HOLD_REASON_PREFIX = "readiness hold: "


def hold_reason(unmet: list[str]) -> str:
    """Format ``unmet`` reasons into the ``deferred_reason`` the gate writes."""
    return HOLD_REASON_PREFIX + "; ".join(unmet)


def is_hold_reason(reason: str | None) -> bool:
    """True iff ``reason`` is a ``deferred_reason`` this gate wrote."""
    return reason is not None and reason.startswith(HOLD_REASON_PREFIX)
