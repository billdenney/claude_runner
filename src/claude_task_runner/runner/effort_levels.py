"""TOML-driven effort-level validation per model.

See ADR-0010. Effort levels accepted by Claude Code vary by model and
change as Anthropic adds or removes them. We never hardcode them as a
``Literal`` — they live in ``[effort_levels]`` of the merged settings.

Public surface:

* :func:`validate_effort` — raise :class:`UnknownEffortLevel` if the
  given ``(model, effort)`` is not in the configured set.
* :func:`accepted_efforts` — return the configured list for a model.
* :func:`accepted_models` — return the list of models with configured
  effort sets.
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


class UnknownModel(ValueError):
    """The given model is not in the effort_levels mapping at all."""

    def __init__(self, model: str, known: list[str]) -> None:
        self.model = model
        self.known = known
        super().__init__(
            f"model {model!r} has no effort_levels configured. Known models: {sorted(known)}"
        )


def accepted_efforts(
    model: str,
    effort_levels: dict[str, list[str]],
) -> list[str]:
    """Return the accepted-effort list for a model.

    Raises :class:`UnknownModel` if the model isn't keyed in the mapping.
    """
    if model not in effort_levels:
        raise UnknownModel(model, list(effort_levels))
    return list(effort_levels[model])


def accepted_models(effort_levels: dict[str, list[str]]) -> list[str]:
    """Return all models that have at least one configured effort."""
    return sorted(m for m, lst in effort_levels.items() if lst)


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
