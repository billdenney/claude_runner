"""YAML I/O for the queue.

* ``load_task(path)`` — read a Task YAML.
* ``load_state(path)`` — read a TaskState YAML.
* ``write_state_atomic(state, path)`` — write a TaskState atomically.
* ``list_pending(queue_dir)`` — enumerate pending task YAMLs in ``todo/``.
* ``list_states(queue_dir)`` — enumerate ``.claude_task_runner/state/*.yaml``.

Atomic writes use ``tempfile.NamedTemporaryFile`` + ``os.replace`` so
concurrent reads always see a complete file (key invariant 8 in
``docs/architecture.md``).

Reads parse with LibYAML's ``CSafeLoader`` when PyYAML was built with it
(see :func:`_yaml_loader`): every supervisor tick loads every task YAML
in ``todo/``, and on a 5,294-file queue that is ~11x faster than the
pure-Python ``SafeLoader``.
"""

from __future__ import annotations

import functools
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, TypeAlias, TypeVar, cast

import yaml
from pydantic import BaseModel, ValidationError
from yaml.composer import ComposerError
from yaml.nodes import Node
from yaml.resolver import BaseResolver

from claude_task_runner.queue.schema import (
    CURRENT_SCHEMA_VERSION,
    Task,
    TaskState,
)

T = TypeVar("T", bound=BaseModel)

MAX_YAML_BYTES = 1 * 1024 * 1024
"""Upper bound on a queue YAML file's size. Task/state YAMLs are a few
KB at most; a file this large is pathological (an accident or a crafted
billion-laughs-style payload) and is rejected before the YAML parser
gets a chance to expand it and stall the tick."""

MAX_YAML_DEPTH = 64
"""Upper bound on how deeply a queue YAML's nodes nest. The deepest
schema-valid document is five levels (``TaskState.runs[].usage.<field>``),
so no file that could validate comes near it. The bound exists because
both loaders recurse once per level while composing, so an unbounded
deep document makes ``SafeLoader`` raise ``RecursionError`` (~490
levels) and makes ``CSafeLoader`` overflow the C stack and SEGFAULT the
process (~26,000 levels on an 8 MiB stack, i.e. a 26 KB file, far under
:data:`MAX_YAML_BYTES`). Like :data:`MAX_YAML_BYTES` this is a
parser-safety bound, not an operator setting (ADR-0014): raising it far
enough would bring the segfault back."""


class QueueIOError(OSError):
    """Underlying I/O failed."""


class QueueSchemaError(ValueError):
    """A YAML file does not validate against the v2 schema."""


def queue_runtime_dir(queue_dir: Path) -> Path:
    """Resolve ``<queue>/.claude_task_runner/`` and ensure it exists."""
    runtime = queue_dir / ".claude_task_runner"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "state").mkdir(exist_ok=True)
    (runtime / "sidecar").mkdir(exist_ok=True)
    (runtime / "logs").mkdir(exist_ok=True)
    return runtime


def todo_dir(queue_dir: Path) -> Path:
    """Resolve ``<queue>/todo/`` and ensure it exists."""
    todo = queue_dir / "todo"
    todo.mkdir(parents=True, exist_ok=True)
    return todo


def state_dir(queue_dir: Path) -> Path:
    """Resolve the per-task state directory under runtime."""
    return queue_runtime_dir(queue_dir) / "state"


def _check_schema_version(payload: dict[str, Any], path: Path) -> None:
    sv = payload.get("schema_version")
    if sv is None:
        # Older or unversioned files — record the omission but let pydantic
        # apply the v2 default. Operators should add the field manually.
        return
    if sv != CURRENT_SCHEMA_VERSION:
        raise QueueSchemaError(
            f"{path}: schema_version={sv} does not match supported version {CURRENT_SCHEMA_VERSION}"
        )


class _DepthLimit(BaseResolver):
    """Loader mixin that rejects nesting deeper than :data:`MAX_YAML_DEPTH`.

    Both composers, PyYAML's pure-Python one and LibYAML's Cython one,
    call ``descend_resolver`` before composing each node and
    ``ascend_resolver`` after it, so counting here stops the recursion
    long before it gets dangerous, whichever loader is in use.
    """

    _depth = 0

    def descend_resolver(self, current_node: Node | None, current_index: Node | int | None) -> None:
        self._depth += 1
        if self._depth > MAX_YAML_DEPTH:
            # The hook sees only the parent, so point at the collection
            # being composed; the too-deep node starts inside it.
            raise ComposerError(
                "while composing a collection",
                None if current_node is None else current_node.start_mark,
                f"found a node nested more than {MAX_YAML_DEPTH} levels deep (MAX_YAML_DEPTH)",
            )
        super().descend_resolver(current_node, current_index)

    def ascend_resolver(self) -> None:
        super().ascend_resolver()
        self._depth -= 1


# Quoted so it is never evaluated: a PyYAML built without LibYAML has no
# yaml.CSafeLoader, and this module must still import there.
_SafeLoaderClass: TypeAlias = "type[yaml.SafeLoader] | type[yaml.CSafeLoader]"


@functools.cache
def _depth_limited(base: _SafeLoaderClass) -> _SafeLoaderClass:
    """``base`` with :class:`_DepthLimit` mixed in, built once per base."""
    return cast(_SafeLoaderClass, type(f"_DepthLimited{base.__name__}", (_DepthLimit, base), {}))


def _yaml_loader() -> _SafeLoaderClass:
    """The loader class ``_load_yaml`` parses with.

    LibYAML's ``CSafeLoader`` when PyYAML was built with it (the
    manylinux wheels are), else the pure-Python ``SafeLoader``. Both run
    PyYAML's Python resolver and constructor, so scalars resolve and
    objects build identically; only the scanners differ, and only on
    hand-written edge cases. ``CSafeLoader`` accepts a tab as separating
    whitespace (``key:<TAB>value``, a trailing tab), which ``SafeLoader``
    rejects; it rejects surrogate escapes (``"\\ud83d"``), ``%YAML``
    versions other than 1.1/1.2 and unknown ``%`` directives, which
    ``SafeLoader`` accepts. ``TestLoaderDivergence`` in
    ``tests/unit/test_store.py`` pins each difference.

    Resolved per call, so a test can simulate a PyYAML built without
    LibYAML by deleting ``yaml.CSafeLoader``.
    """
    return _depth_limited(getattr(yaml, "CSafeLoader", yaml.SafeLoader))


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        size = path.stat().st_size
        if size > MAX_YAML_BYTES:
            raise QueueSchemaError(
                f"{path}: file is {size} bytes, exceeds limit of {MAX_YAML_BYTES} bytes"
            )
        with path.open("rb") as fh:
            # Safe: _yaml_loader() is always a SafeLoader or CSafeLoader.
            data = yaml.load(fh, Loader=_yaml_loader())
    except OSError as exc:
        raise QueueIOError(f"failed to read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise QueueSchemaError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise QueueSchemaError(f"{path}: top-level YAML must be a mapping")
    return data


def _validate(model: type[T], payload: dict[str, Any], path: Path) -> T:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        # Translate the raw pydantic error into actionable authoring guidance
        # (unknown/missing/enum fields + a pointer to `queue template`). Bad
        # task YAMLs are a recurring authoring failure; a fixable message is
        # worth more than the pydantic dump.
        from .help import explain_validation_error

        raise QueueSchemaError(explain_validation_error(exc, path, model)) from exc


def load_task(path: Path) -> Task:
    """Read and validate a single Task YAML."""
    payload = _load_yaml(path)
    _check_schema_version(payload, path)
    return _validate(Task, payload, path)


def load_state(path: Path) -> TaskState:
    """Read and validate a single TaskState YAML."""
    payload = _load_yaml(path)
    _check_schema_version(payload, path)
    return _validate(TaskState, payload, path)


def _model_to_yaml_dict(model: BaseModel) -> dict[str, Any]:
    """Pydantic-aware YAML dict: dump with mode='json' so datetimes /
    paths / enums become strings serializable by PyYAML."""
    return model.model_dump(mode="json", exclude_none=False)


def write_state_atomic(state: TaskState, path: Path) -> None:
    """Write a TaskState YAML via tempfile + ``os.replace``.

    Concurrent readers either see the previous version or the new one —
    never a torn file. ``path``'s parent must exist.
    """
    parent = path.parent
    if not parent.exists():
        raise QueueIOError(f"parent dir does not exist: {parent}")

    payload = _model_to_yaml_dict(state)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=parent,
        delete=False,
        prefix=f".{path.name}.",
        suffix=".tmp",
    ) as tmp:
        yaml.safe_dump(payload, tmp, sort_keys=False, default_flow_style=False)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def write_task_atomic(task: Task, path: Path) -> None:
    """Write a Task YAML atomically. Mirrors :func:`write_state_atomic`."""
    parent = path.parent
    if not parent.exists():
        raise QueueIOError(f"parent dir does not exist: {parent}")

    payload = _model_to_yaml_dict(task)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=parent,
        delete=False,
        prefix=f".{path.name}.",
        suffix=".tmp",
    ) as tmp:
        yaml.safe_dump(payload, tmp, sort_keys=False, default_flow_style=False)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def list_pending_tasks(queue_dir: Path) -> Iterator[Path]:
    """Yield paths of every task YAML under ``<queue>/todo/`` in sorted order.

    Sorting by filename gives a deterministic dispatch order matching the
    operator's chosen ID prefixes (``001-``, ``002-``, ...).
    """
    todo = todo_dir(queue_dir)
    yield from sorted(todo.glob("*.yaml"))


def list_state_files(queue_dir: Path) -> Iterator[Path]:
    """Yield paths of every TaskState YAML in sorted order."""
    yield from sorted(state_dir(queue_dir).glob("*.yaml"))


def state_path_for(queue_dir: Path, task_id: str) -> Path:
    """Conventional path for a given task's state YAML."""
    return state_dir(queue_dir) / f"{task_id}.yaml"


def task_path_for(queue_dir: Path, task_id: str) -> Path:
    """Conventional path for a given task's input YAML under ``todo/``."""
    return todo_dir(queue_dir) / f"{task_id}.yaml"
