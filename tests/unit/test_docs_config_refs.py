"""Docs must not document config keys the schema rejects.

Every settings model is ``extra="forbid"``, so a key the docs tell an
operator to set but the schema does not define makes the *whole*
``claude_runner.toml`` fail to load. Two such keys shipped after the
2026-06-13 dead-config audit deleted their tables:
``[sidecar].unanswered_auto_recommended_s`` in ``docs/runbook.md`` and
``[notify].channels`` in ``docs/architecture.md``. Both were prose-only
casualties of that removal.

``[throttle.*]`` -- the one table retired *with* a mechanical guard
(``config.loader._reject_legacy_throttle``) -- is also the one whose docs
stayed accurate. This test is that guard for the rest of the docs.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, get_args, get_origin

import pytest
from pydantic import BaseModel

from claude_task_runner.config.schema import Settings

REPO_ROOT = Path(__file__).parent.parent.parent
DOCS_DIR = REPO_ROOT / "docs"

RETIRED_TABLES = {
    "throttle": (
        "ADR-0022 replaced [throttle.*] with [dispatch_pct.*]. Docs reference "
        "it deliberately: superseded ADRs 0015/0016 record it as history and "
        "docs/cheatsheet.md carries the old-to-new migration table. "
        "config.loader._reject_legacy_throttle hard-errors on the key, so an "
        "operator cannot silently resurrect it."
    ),
}
"""Tables that no longer exist but which docs may still name.

Add an entry ONLY for a table that docs describe as retired/historical.
A table an operator might still be told to *set* does not belong here --
that is the bug this test exists to catch.
"""

_PLACEHOLDER = re.compile(r"^(?:\*|<[^>]*>|\.\.\.|N|NNN)$")
"""A doc placeholder segment (``<model>``, ``*``). Unverifiable, so it
terminates matching successfully rather than failing a real path."""

_INLINE_REF = re.compile(r"\[([a-z_][\w.]*)\]\.([a-z_][\w.<>]*)")
"""``[table].field`` written in prose -- the shape of both real bugs."""

_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_SPAN_REF = re.compile(r"^\[\[?([a-z_][\w.]*?)(?:\.\*)?\]?\](?:\.([a-z_][\w.<>]*))?$")
"""A whole code span that is a config reference: `[queue]`,
`[hooks].pre_dispatch_command`, `[ema.priors.<model>.<effort>]`."""

_FENCE = re.compile(r"^```+\s*([a-zA-Z0-9_-]*)\s*$")
_TOML_TABLE = re.compile(r"^\[\[?([a-z_][\w.]*)\]?\]$")
_TOML_KEY = re.compile(r"^([a-z_][\w]*)\s*=")


def _matches(annotation: Any, segments: tuple[str, ...]) -> bool:
    """Walk ``segments`` down a pydantic annotation.

    Empty ``segments`` means the path resolved -- a bare table mention
    such as ``[queue]`` is a valid reference. Returns False as soon as a
    segment names something the model does not define.
    """
    if not segments:
        return True
    head, rest = segments[0], segments[1:]
    if _PLACEHOLDER.match(head):
        return True

    origin = get_origin(annotation)
    if origin is dict:
        # ``head`` is an operator-chosen key (a model name, a plan name);
        # descend into the value type with it consumed.
        _key_type, value_type = get_args(annotation)
        return _matches(value_type, rest)
    if origin is not None:
        # Union / Optional / list -- any member that resolves is enough.
        return any(_matches(arg, segments) for arg in get_args(annotation))
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        field = annotation.model_fields.get(head)
        if field is None:
            return False
        return _matches(field.annotation, rest)
    return False


def _is_known(path: str) -> bool:
    return _matches(Settings, tuple(path.split(".")))


def _iter_doc_refs(text: str) -> list[tuple[int, str]]:
    """Yield ``(line_number, dotted_path)`` for every config reference.

    Covers three shapes: ``[table].field`` in prose, a code span that is
    wholly a config reference, and the table headers / keys inside a
    fenced ``toml`` block (the copy-paste path an operator is most
    likely to follow verbatim).
    """
    # A backticked `[table].field` matches both the inline and the
    # code-span pattern; dict keys keep first-seen order and dedupe it.
    refs: dict[tuple[int, str], None] = {}
    fence_lang: str | None = None
    toml_table = ""

    for lineno, line in enumerate(text.splitlines(), 1):
        fence = _FENCE.match(line.strip())
        if fence is not None:
            if fence_lang is None:
                fence_lang, toml_table = fence.group(1).lower(), ""
            else:
                fence_lang = None
            continue

        if fence_lang is not None:
            if fence_lang != "toml":
                continue
            stripped = line.split("#", 1)[0].strip()
            if not stripped:
                continue
            table_match = _TOML_TABLE.match(stripped)
            if table_match is not None:
                toml_table = table_match.group(1)
                refs[(lineno, toml_table)] = None
                continue
            key = _TOML_KEY.match(stripped)
            if key is not None and toml_table:
                refs[(lineno, f"{toml_table}.{key.group(1)}")] = None
            continue

        for match in _INLINE_REF.finditer(line):
            refs[(lineno, f"{match.group(1)}.{match.group(2)}")] = None
        for span in _CODE_SPAN.finditer(line):
            span_ref = _SPAN_REF.match(span.group(1).strip())
            if span_ref is None:
                continue
            span_table, span_field = span_ref.group(1), span_ref.group(2)
            refs[(lineno, f"{span_table}.{span_field}" if span_field else span_table)] = None

    return list(refs)


def _doc_files() -> list[Path]:
    return sorted(DOCS_DIR.rglob("*.md"))


class TestSchemaWalker:
    """The matcher itself -- a broken checker would pass everything."""

    def test_resolves_nested_path(self) -> None:
        assert _is_known("dispatch_pct.day.fivehr_stop_pct")

    def test_resolves_bare_table(self) -> None:
        assert _is_known("queue")

    def test_resolves_through_list_of_models(self) -> None:
        assert _is_known("accounts.config_dir")

    def test_resolves_through_nested_dict_keys(self) -> None:
        # priors is dict[str, dict[str, EMAPrior]]: two operator-chosen keys.
        assert _is_known("ema.priors.opus.high.duration_s")

    def test_placeholder_segment_terminates(self) -> None:
        assert _is_known("ema.priors.<model>.<effort>")

    def test_rejects_deleted_table(self) -> None:
        assert not _is_known("sidecar.unanswered_auto_recommended_s")
        assert not _is_known("notify.channels")

    def test_rejects_unknown_field_on_real_table(self) -> None:
        assert not _is_known("queue.no_such_field")

    def test_rejects_field_on_scalar(self) -> None:
        assert not _is_known("concurrency.max_concurrency.nope")


class TestDocRefExtraction:
    def test_extracts_inline_ref(self) -> None:
        assert (1, "hooks.pre_dispatch_command") in _iter_doc_refs(
            "set `[hooks].pre_dispatch_command` and go"
        )

    def test_extracts_bare_code_span_table(self) -> None:
        assert (1, "failure_classifier") in _iter_doc_refs(
            "edit `[failure_classifier]` in the toml"
        )

    def test_extracts_from_toml_fence(self) -> None:
        block = '```toml\n[dispatch_pct.week]\neow_time_switch = "48h"\n```'
        refs = _iter_doc_refs(block)
        assert (2, "dispatch_pct.week") in refs
        assert (3, "dispatch_pct.week.eow_time_switch") in refs

    def test_ignores_non_toml_fence(self) -> None:
        assert _iter_doc_refs("```bash\n[notify].channels\n```") == []

    def test_ignores_markdown_link(self) -> None:
        assert _iter_doc_refs("see [the runbook](runbook.md) for more") == []


class TestDocsMatchSchema:
    def test_docs_dir_is_present(self) -> None:
        # Guards the whole suite: a wrong root would make every
        # parametrised case vanish and the gate would pass on 0 files.
        assert DOCS_DIR.is_dir(), f"docs/ not found at {DOCS_DIR}"
        assert _doc_files(), "no markdown found under docs/"

    @pytest.mark.parametrize("doc", _doc_files(), ids=lambda p: p.name)
    def test_every_config_reference_exists(self, doc: Path) -> None:
        bad: list[str] = []
        for lineno, path in _iter_doc_refs(doc.read_text()):
            if path.split(".")[0] in RETIRED_TABLES or _is_known(path):
                continue
            table, _, field = path.partition(".")
            shown = f"[{table}].{field}" if field else f"[{table}]"
            bad.append(f"  {doc.relative_to(REPO_ROOT)}:{lineno}: {shown}")
        assert not bad, (
            "Config reference(s) in docs that the schema does not define.\n"
            'Every settings model is extra="forbid", so an operator '
            "following these would get a config that refuses to load.\n"
            + "\n".join(bad)
            + "\nFix the docs, add the field to config/schema.py, or -- only "
            "for a table docs describe as retired -- add it to "
            "RETIRED_TABLES in this file."
        )
