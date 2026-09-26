"""TOML-driven effort-level validation per model.

See ADR-0010. Effort levels accepted by Claude Code vary by model and
change as Anthropic adds or removes them. We never hardcode them as a
``Literal`` — they live in ``[effort_levels]`` of the merged settings.

Public surface:

* :func:`validate_effort` — raise :class:`UnknownEffortLevel` if the
  given ``(model, effort)`` is not in the configured set, including a
  model with no configured set at all.
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
                f"add a [effort_levels.{model!r}] entry to "
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
