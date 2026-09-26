"""Session resume logic — try ``claude --resume <id>`` then fall back to fresh.

See ADR-0005. The resume path is:

1. If the task has a ``session_id`` AND a session JSONL exists at the
   conventional Claude Code location, try ``claude --resume <id>`` with
   a continuation prompt.
2. Every resume attempt, whatever its outcome, increments
   ``TaskState.resume_attempts``. A resume that fails is not retried
   fresh within the same attempt; the next attempt is planned again.
3. Once ``resume_attempts`` reaches ``[session].max_resume_attempts``,
   only fresh dispatches are attempted.

This module decides **which strategy to use** and constructs the argv
for the dispatcher. The actual subprocess management is in
:mod:`runner.dispatcher`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from claude_task_runner.config.schema import SessionSettings
from claude_task_runner.queue.schema import Task, TaskState


class ResumeStrategy(StrEnum):
    """Which spawn strategy the dispatcher should use for the next attempt."""

    FRESH = "fresh"
    """Run with the original prompt — no ``--resume``."""

    RESUME = "resume"
    """Run with ``claude --resume <session_id>`` and a continuation prompt."""


@dataclass(frozen=True)
class SpawnPlan:
    """Argv-building input for :mod:`runner.dispatcher`.

    Attributes
    ----------
    strategy
        FRESH or RESUME.
    session_id
        Set on RESUME; ``None`` on FRESH.
    prompt
        The text to pass via ``--print``. For FRESH this is ``task.prompt``;
        for RESUME a short continuation marker.
    extra_args
        Optional list of additional args (e.g. ``--model``,
        ``--allowedTools``) the dispatcher should pass through.
    """

    strategy: ResumeStrategy
    session_id: str | None
    prompt: str
    extra_args: list[str]


CONTINUATION_PROMPT = "Continue where you left off."
"""The prompt sent on a successful ``--resume`` to nudge the model
forward without restating the full task."""


def session_jsonl_exists(
    session_id: str,
    *,
    claude_projects_dir: Path | None = None,
) -> bool:
    """True iff a JSONL for ``session_id`` is found under the projects dir.

    Scans recursively because Claude Code namespaces sessions by
    project directory; the project dir name is a slug we can't recover
    from the session id alone.
    """
    base = (
        claude_projects_dir
        if claude_projects_dir is not None
        else Path.home() / ".claude" / "projects"
    )
    if not base.exists():
        return False
    target = f"{session_id}.jsonl"
    for project_dir in base.iterdir():
        if not project_dir.is_dir():
            continue
        if (project_dir / target).exists():
            return True
    return False


def plan_next_spawn(
    task: Task,
    state: TaskState,
    *,
    settings: SessionSettings,
    extra_args: list[str] | None = None,
    claude_projects_dir: Path | None = None,
) -> SpawnPlan:
    """Decide whether the next attempt resumes or starts fresh.

    Decision tree:

    * No ``state.session_id`` → FRESH.
    * ``state.resume_attempts >= max_resume_attempts`` → FRESH (the
      dispatcher counts every resume attempt, successful or not).
    * Session JSONL doesn't exist on disk → FRESH.
    * Otherwise → RESUME.

    There is no fast fall-through: a ``--resume`` that fails counts
    toward the cap like any other, and the next attempt is planned here.
    """
    args = list(extra_args or [])

    if not state.session_id:
        return SpawnPlan(
            strategy=ResumeStrategy.FRESH,
            session_id=None,
            prompt=task.prompt,
            extra_args=args,
        )

    if state.resume_attempts >= settings.max_resume_attempts:
        return SpawnPlan(
            strategy=ResumeStrategy.FRESH,
            session_id=None,
            prompt=task.prompt,
            extra_args=args,
        )

    if not session_jsonl_exists(state.session_id, claude_projects_dir=claude_projects_dir):
        return SpawnPlan(
            strategy=ResumeStrategy.FRESH,
            session_id=None,
            prompt=task.prompt,
            extra_args=args,
        )

    return SpawnPlan(
        strategy=ResumeStrategy.RESUME,
        session_id=state.session_id,
        prompt=CONTINUATION_PROMPT,
        extra_args=args,
    )
