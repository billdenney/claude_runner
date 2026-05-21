"""Resolve the ``--add-dir`` list passed to each dispatched ``claude``.

Claude Code's --print mode sandboxes the spawned agent to its ``cwd``;
reading or writing paths outside that scope is silently blocked. Tasks
in this runner routinely need access to three classes of out-of-cwd
paths: the queue's source files (papers/, from_people/, ...), the
queue's runtime state (.claude_task_runner/sidecar/, reports/), and
ad-hoc per-task extras the operator declared in the task YAML. The
``claude`` CLI exposes ``--add-dir <path>`` to widen the allowed
scope; this module is the single place that decides what gets passed.

Three sources, intersected and deduplicated:

* The queue directory itself (always-on). The sidecar protocol, the
  operator-deliverable reports/, and most source file layouts live
  here, so without this the runner is broken by default.
* ``task.additional_dirs`` from the task YAML (always honoured).
  Per-task explicit declaration; the operator's most precise lever.
* (Opt-in) ``[dispatch].auto_detect_paths_in_prompt`` — extract
  absolute paths from the prompt text and add the ones that resolve
  to existing directories. Off by default because false positives are
  common in prose-y prompts.

Missing paths emit a warning and are dropped; dispatch is not failed.
The runner's promise is "best-effort widen the sandbox"; if the
operator typoed a path, we'd rather dispatch with the wrong scope and
let the agent's first blocked read surface the typo via the normal
error path than refuse to dispatch and stall the queue.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from claude_task_runner.queue.schema import Task

logger = logging.getLogger(__name__)

_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w])/[A-Za-z0-9._/-]+")
"""Match a substring starting with ``/`` that looks like a filesystem
path. Deliberately restrictive: only letters, digits, dot, underscore,
slash, and hyphen. The negative lookbehind ``(?<![\\w])`` prevents
matching mid-word slashes inside URLs or already-quoted strings."""


def _normalize(path: Path) -> Path:
    """Return the resolved, absolute form for dedup comparisons.

    ``Path.resolve(strict=False)`` collapses ``..`` and symlinks where
    possible without raising on missing components. We don't need
    ``strict=True`` here because the existence check is a separate
    step.
    """
    try:
        return path.expanduser().resolve(strict=False)
    except OSError:
        return path.expanduser().absolute()


def _existing_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _detect_paths_in_prompt(prompt: str) -> list[Path]:
    """Extract absolute paths from prompt text, deduplicated in input order.

    The regex captures candidate substrings; the caller filters them
    to existing directories.
    """
    seen: set[str] = set()
    out: list[Path] = []
    for match in _ABSOLUTE_PATH_RE.finditer(prompt):
        token = match.group(0).rstrip(".,;:!?)")
        if token in seen:
            continue
        seen.add(token)
        out.append(Path(token))
    return out


def resolve_add_dirs(
    task: Task,
    queue_dir: Path,
    *,
    auto_detect: bool = False,
) -> list[Path]:
    """Return the deduplicated, validated list of dirs to pass via ``--add-dir``.

    Order is stable and meaningful for log readability:

    1. ``queue_dir`` (always first; the always-on entry).
    2. ``task.additional_dirs`` entries that resolve to existing dirs
       (in declared order, dedup'd against the queue dir and each
       other).
    3. If ``auto_detect`` is True, absolute paths found in
       ``task.prompt`` that resolve to existing directories (in first-
       seen order, dedup'd against everything above).

    Non-existent or non-directory paths in ``task.additional_dirs``
    emit a warning and are skipped. Auto-detected non-matches are
    silently skipped (the regex catches many strings that aren't
    real dirs; warning each would be noise).
    """
    queue_norm = _normalize(queue_dir)
    out: list[Path] = []
    seen_norm: set[Path] = set()

    if _existing_dir(queue_norm):
        out.append(queue_norm)
        seen_norm.add(queue_norm)
    else:
        logger.warning(
            "task %s: queue_dir %s does not exist; --add-dir will not be widened to it",
            task.id,
            queue_dir,
        )

    for raw in task.additional_dirs:
        norm = _normalize(raw)
        if norm in seen_norm:
            continue
        if not _existing_dir(norm):
            logger.warning(
                "task %s: additional_dirs entry %s is missing or not a directory; skipping",
                task.id,
                raw,
            )
            continue
        out.append(norm)
        seen_norm.add(norm)

    if auto_detect:
        for candidate in _detect_paths_in_prompt(task.prompt):
            # Prompts typically reference *files* by absolute path
            # (e.g. ``/queue/papers/X/X.pdf``); the agent needs read
            # access to the *containing directory*. Walk up until we
            # hit an existing directory, then dedup.
            walked = _walk_to_existing_dir(_normalize(candidate))
            if walked is None:
                continue
            if walked in seen_norm:
                continue
            out.append(walked)
            seen_norm.add(walked)

    return out


def _walk_to_existing_dir(start: Path) -> Path | None:
    """Climb ``start``'s parent chain until we find an existing directory.

    Returns ``None`` if no ancestor exists or if the ancestors we'd
    discover are too coarse (``/`` and the cwd's first level are
    skipped to avoid the auto-detect path widening scope to the
    entire filesystem on a stray prompt token).
    """
    candidate: Path | None = start
    while candidate is not None:
        if _existing_dir(candidate):
            # Block the bare root and the immediate top-level dir
            # (``/home``, ``/var``, etc.) — too coarse to be a useful
            # widening, almost certainly a regex false positive.
            if candidate == candidate.parent:
                return None
            if candidate.parent == Path(candidate.parent.anchor):
                return None
            return candidate
        candidate = candidate.parent if candidate.parent != candidate else None
    return None
