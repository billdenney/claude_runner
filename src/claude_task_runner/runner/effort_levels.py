"""TOML-driven effort-level validation per model.

See ADR-0010. Effort levels accepted by Claude Code vary by model and
change as Anthropic adds or removes them. We never hardcode them as a
``Literal`` — they live in ``[effort_levels]`` of the merged settings.

Public surface:

* :func:`validate_effort` — raise :class:`UnknownEffortLevel` if the
  given ``(model, effort)`` is not in the configured set, including a
  model with no configured set at all.
* :func:`hold_reason` / :func:`is_hold_reason` — the ``deferred_reason``
  the supervisor writes when it parks a task whose pair fails
  :func:`validate_effort`, and the test for one it wrote.

The task schema cannot run :func:`validate_effort`, because the accepted
sets come from the merged settings, which ``queue.store.load_task`` never
sees. So each place that has the settings in hand checks the pair itself:
``queue add`` before it writes a task, the supervisor's candidate
selector before it dispatches one (parking a mismatched task as
``deferred``, see :func:`hold_reason`), both force-dispatch paths, the
doctor's ``task_yamls`` check and ``queue list``.
"""

from __future__ import annotations


class UnknownEffortLevel(ValueError):
    """The given effort is not configured for the given model.

    Carries both ``model`` and ``effort`` so callers can build a useful
    error message including the accepted set.
    """

    def __init__(self, model: str, effort: str, accepted: list[str] | None) -> None:
        self.model = model
        self.effort = effort
        self.accepted = accepted
        if accepted is None:
            super().__init__(
                f"model {model!r} has no effort levels configured; "
                f'add "{model}" = [<levels>] under [effort_levels] in '
                "claude_runner.toml or use a configured model"
            )
        else:
            super().__init__(
                f"effort {effort!r} not in accepted set for model {model!r}: {sorted(accepted)}"
            )


def validate_effort(
    model: str,
    effort: str,
    effort_levels: dict[str, list[str]],
) -> None:
    """Raise :class:`UnknownEffortLevel` if the (model, effort) pair is
    not configured.

    A missing model entry is treated as "no effort levels configured" —
    the error message guides the operator to add a ``[effort_levels]``
    entry.
    """
    if model not in effort_levels:
        raise UnknownEffortLevel(model, effort, accepted=None)
    accepted = effort_levels[model]
    if effort not in accepted:
        raise UnknownEffortLevel(model, effort, accepted=accepted)


HOLD_REASON_PREFIX = "invalid effort: "
"""Prefix of the ``deferred_reason`` the supervisor writes when it parks a
task whose ``(model, effort)`` pair fails :func:`validate_effort`.

The supervisor un-parks only a task whose reason carries this prefix, so a
pre-dispatch hook's deferral, a readiness hold and an operator's manual
park are never cleared by the effort check."""


def hold_reason(exc: UnknownEffortLevel) -> str:
    """Format ``exc`` into the ``deferred_reason`` the supervisor writes."""
    return HOLD_REASON_PREFIX + str(exc)


def is_hold_reason(reason: str | None) -> bool:
    """True iff ``reason`` is a ``deferred_reason`` :func:`hold_reason` wrote."""
    return reason is not None and reason.startswith(HOLD_REASON_PREFIX)
