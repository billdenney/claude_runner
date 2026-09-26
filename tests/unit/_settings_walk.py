"""Walk the settings models that an operator's two TOML files load into.

Shared by the gates that quantify over every setting:
``test_settings_readers`` (every field has a reader),
``test_settings_finite`` (no float field accepts ``inf`` or ``nan``) and
``test_settings_bounds`` (no duration exceeds ten years). The last two also
share how a test writes one setting into a file and loads it. Each gate
keeps known-answer tests of what it takes from here.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple, get_args, get_origin

from pydantic import BaseModel

from claude_task_runner.config.loader import (
    PER_ACCOUNT_TOML_NAME,
    load_account_policy,
    load_settings,
)
from claude_task_runner.config.schema import AccountPolicy, Settings

ROOTS: dict[str, type[BaseModel]] = {
    "claude_runner.toml": Settings,
    "runner-account.toml": AccountPolicy,
}
"""The two files an operator writes, and the model each one loads into."""

LOADERS: dict[str, Callable[[Path], object]] = {
    "claude_runner.toml": lambda directory: load_settings(directory / "claude_runner.toml"),
    PER_ACCOUNT_TOML_NAME: lambda directory: load_account_policy(str(directory)),
}
"""For each file in ``ROOTS``: how the runner loads it from its directory."""


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


def holds_float(annotation: Any) -> bool:
    """Whether a field with this annotation can hold a ``float``.

    Looks through ``Optional``, unions, lists, dicts and ``Annotated``. A
    nested model is not a float: the walk visits its fields separately.
    """
    if isinstance(annotation, type) and issubclass(annotation, float):
        return True
    return any(holds_float(arg) for arg in get_args(annotation))


def float_paths(root: type[BaseModel]) -> list[str]:
    """The TOML path of every field under ``root`` that can hold a float."""
    return [
        field.path
        for field in walk_settings(root)
        if holds_float(field.model.model_fields[field.name].annotation)
    ]


def toml_setting(path: str, value: str) -> str:
    """A TOML document that sets the dotted ``path`` to ``value``.

    Writes a key in nested tables, which is where every numeric setting is
    today. A setting under ``[[accounts]]`` or a ``<key>`` table needs more,
    and a gate would then fail on the error's location: extend this.
    """
    table, _, key = path.rpartition(".")
    header = f"[{table}]\n" if table else ""
    return f"{header}{key} = {value}\n"
