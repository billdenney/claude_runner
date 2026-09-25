"""Every settings field must have a reader in the runtime code.

Every settings model is ``extra="forbid"``, so an operator can set any
field the schema declares, and a field no code reads is an option that
silently does nothing. The 2026-06-13 audit found five tables of them by
hand and deleted them. By 2026-09-25 more had built up, some documented
as live: ``[claude].plan``, ``[plans.*]``, ``[ema]``,
``[usage].healthcheck_interval_s``, ``[usage].suspicious_delta_pct`` and
``[session].resume_fail_fast_s``. This test is that audit, run on every
commit.

A field counts as read when its name appears anywhere in
``src/claude_task_runner`` outside ``config/schema.py`` (which only
declares fields) as a loaded attribute (``settings.usage.poll_interval_s``)
or as the literal name in ``getattr(obj, "name")``. Docstrings and
comments do not count. The match is by name alone, so an unrelated
attribute that shares a dead field's name hides it: the test errs toward
passing. It also cannot tell whether the reading code ever runs;
``[ema].prior_warmup_samples`` was read, by a module nothing called.
"""

from __future__ import annotations

import ast
import functools
from pathlib import Path
from typing import Any, get_args, get_origin

from pydantic import BaseModel

from claude_task_runner.config.schema import AccountPolicy, Settings

REPO_ROOT = Path(__file__).parent.parent.parent
SRC_DIR = REPO_ROOT / "src" / "claude_task_runner"
SCHEMA_FILE = SRC_DIR / "config" / "schema.py"

ROOTS: dict[str, type[BaseModel]] = {
    "claude_runner.toml": Settings,
    "runner-account.toml": AccountPolicy,
}
"""The two files an operator writes, and the model each one loads into."""

KNOWN_UNREAD: dict[str, str] = {
    "SessionSettings.resume_fail_fast_s": (
        "ADR-0005's fast resume fall-through was never built; runner.dispatcher "
        "only names runner.session.fall_through_to_fresh 'for static analysis'"
    ),
    "Settings.ema": "no runtime code reads settings.ema; runner/ema.py is reached only from tests",
    "EMASettings.alpha": "a parameter of runner.ema.update_bucket, which nothing calls",
    "EMAPrior.tokens": "read by a computed getattr in runner.ema, reached only from tests",
    "EMAPrior.duration_s": "see EMAPrior.tokens",
}
"""``"Model.field"`` entries exempt from the check, each with its reason.

The entries below are the dead settings this gate found when it was
added; each is deleted, with a loader guard, and its entry with it.
After that, an entry belongs here only for a field that is read in a way
the scan cannot see (a computed ``getattr``, ``model_dump()``) -- never
for a field nothing reads.
"""


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


def _settings_fields(root: type[BaseModel]) -> dict[str, str]:
    """``{"Model.field": "dotted.toml.path"}`` for every field under ``root``.

    Breadth-first, so a model reachable by more than one path is walked
    once, under its shortest path.
    """
    fields: dict[str, str] = {}
    seen: set[type[BaseModel]] = set()
    pending: list[tuple[type[BaseModel], str]] = [(root, "")]
    while pending:
        model, prefix = pending.pop(0)
        if model in seen:
            continue
        seen.add(model)
        for name, info in model.model_fields.items():
            path = f"{prefix}.{name}" if prefix else name
            fields[f"{model.__name__}.{name}"] = path
            pending.extend(_models_in(info.annotation, path))
    return fields


def _shown(path: str) -> str:
    """``usage.poll_interval_s`` as an operator writes it: ``[usage].poll_interval_s``."""
    table, _, field = path.rpartition(".")
    return f"[{table}].{field}" if table else f"[{field}]"


def _all_fields() -> dict[str, str]:
    """``{"Model.field": "<file>: [table].field"}`` across both operator files."""
    return {
        key: f"{file}: {_shown(path)}"
        for file, root in ROOTS.items()
        for key, path in _settings_fields(root).items()
    }


def _read_names(source: str) -> set[str]:
    """Names ``source`` reads as an attribute or by a literal ``getattr``."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            names.add(node.attr)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            names.add(node.args[1].value)
    return names


def _runtime_sources() -> list[Path]:
    """Every module of the package except the schema that declares the fields."""
    return sorted(path for path in SRC_DIR.rglob("*.py") if path != SCHEMA_FILE)


@functools.cache
def _runtime_reads() -> frozenset[str]:
    return frozenset().union(*(_read_names(path.read_text()) for path in _runtime_sources()))


class _Leaf(BaseModel):
    depth: int


class _Nested(BaseModel):
    table: dict[str, dict[str, _Leaf]]


class _Wrapped(BaseModel):
    maybe: _Leaf | None = None


class TestInstrument:
    """The walker and the scanner themselves -- a broken one passes everything."""

    def test_walks_nested_models(self) -> None:
        assert _settings_fields(Settings)["ClaudeSettings.executable"] == "claude.executable"

    def test_walks_through_a_list(self) -> None:
        # accounts is list[AccountSettings]: an array of tables.
        assert _settings_fields(Settings)["AccountSettings.config_dir"] == "accounts.config_dir"

    def test_walks_through_dict_values(self) -> None:
        assert _settings_fields(_Nested) == {
            "_Nested.table": "table",
            "_Leaf.depth": "table.<key>.<key>.depth",
        }

    def test_walks_through_optional(self) -> None:
        assert _settings_fields(_Wrapped) == {
            "_Wrapped.maybe": "maybe",
            "_Leaf.depth": "maybe.depth",
        }

    def test_walks_the_per_account_file(self) -> None:
        fields = _all_fields()
        assert fields["AccountConcurrencyPolicy.max_concurrency"] == (
            "runner-account.toml: [concurrency].max_concurrency"
        )
        assert fields["UsageSettings.poll_interval_s"] == (
            "claude_runner.toml: [usage].poll_interval_s"
        )

    def test_reads_loaded_attributes_and_literal_getattr(self) -> None:
        source = "a = settings.usage.poll_interval_s\nb = getattr(settings, 'dispatch', None)\n"
        assert _read_names(source) == {"usage", "poll_interval_s", "dispatch"}

    def test_ignores_writes_strings_comments_and_computed_getattr(self) -> None:
        source = (
            '"""settings.in_a_docstring"""\n'
            "settings.written = 1\n"
            "text = 'in_a_string'\n"
            "# settings.in_a_comment\n"
            "value = getattr(settings, name)\n"
        )
        assert _read_names(source) == set()

    def test_scans_the_runtime_but_not_the_schema(self) -> None:
        # Guards the suite: scanning the schema would find every field
        # "read" by its own declaration and pass on nothing.
        sources = _runtime_sources()
        assert SRC_DIR / "supervisor" / "daemon.py" in sources
        assert SCHEMA_FILE.is_file()
        assert SCHEMA_FILE not in sources

    def test_finds_a_known_live_read(self) -> None:
        # supervisor/daemon.py passes settings.usage.poll_interval_s to the poller.
        assert "poll_interval_s" in _runtime_reads()


class TestEverySettingIsRead:
    def test_every_field_has_a_reader(self) -> None:
        reads = _runtime_reads()
        unread = sorted(
            f"  {where}  ({key})"
            for key, where in _all_fields().items()
            if key.split(".", 1)[1] not in reads and key not in KNOWN_UNREAD
        )
        assert not unread, (
            "Settings field(s) that no runtime code reads. Every settings model "
            'is extra="forbid", so an operator can set these and nothing happens.\n'
            + "\n".join(unread)
            + "\nRead the field where it is meant to take effect, or delete it "
            "from config/schema.py and the defaults TOML and add a guard in "
            "config/loader.py so a TOML that still sets it fails with a message "
            "saying to delete it. KNOWN_UNREAD in this file is only for a field "
            "that is read in a way this scan cannot see."
        )

    def test_known_unread_entries_are_current(self) -> None:
        # An entry for a field that is now read, or no longer exists, would
        # quietly exempt the next field to take its name.
        fields = _all_fields()
        reads = _runtime_reads()
        stale = sorted(
            key for key in KNOWN_UNREAD if key not in fields or key.split(".", 1)[1] in reads
        )
        assert not stale, f"KNOWN_UNREAD entries to delete: {stale}"
