"""Walk the settings models that an operator's two TOML files load into.

Shared by the gates that quantify over every setting:
``test_settings_readers`` (every field has a reader) and
``test_settings_finite`` (no float field accepts ``inf`` or ``nan``). Each
gate keeps known-answer tests of what it takes from the walk.
"""

from __future__ import annotations

from typing import Any, NamedTuple, get_args, get_origin

from pydantic import BaseModel

from claude_task_runner.config.schema import AccountPolicy, Settings

ROOTS: dict[str, type[BaseModel]] = {
    "claude_runner.toml": Settings,
    "runner-account.toml": AccountPolicy,
}
"""The two files an operator writes, and the model each one loads into."""


class SettingsField(NamedTuple):
    """One field of a settings model, and the TOML path that sets it."""

    model: type[BaseModel]
    name: str
    path: str
    """Dotted, from the file's root: ``usage.poll_interval_s``. An
    operator-chosen ``dict`` key shows as ``<key>``; a list adds nothing."""


def _models_in(annotation: Any, path: str) -> list[tuple[type[BaseModel], str]]:
    """Every settings model ``annotation`` can hold, with its TOML path.

    Descends through ``dict`` values (an operator-chosen key, shown as
    ``<key>``), lists, ``Optional`` and unions.
    """
    origin = get_origin(annotation)
    if origin is dict:
        _key_type, value_type = get_args(annotation)
        return _models_in(value_type, f"{path}.<key>")
    if origin is not None:
        return [found for arg in get_args(annotation) for found in _models_in(arg, path)]
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [(annotation, path)]
    return []


def walk_settings(root: type[BaseModel]) -> list[SettingsField]:
    """Every field of every model under ``root``, in walk order.

    Breadth-first, so a model reachable by more than one path is walked
    once, under its shortest path.
    """
    fields: list[SettingsField] = []
    seen: set[type[BaseModel]] = set()
    pending: list[tuple[type[BaseModel], str]] = [(root, "")]
    while pending:
        model, prefix = pending.pop(0)
        if model in seen:
            continue
        seen.add(model)
        for name, info in model.model_fields.items():
            path = f"{prefix}.{name}" if prefix else name
            fields.append(SettingsField(model, name, path))
            pending.extend(_models_in(info.annotation, path))
    return fields
