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

from claude_task_runner.config.loader import ConfigError, load_settings
from claude_task_runner.config.schema import Settings

REPO_ROOT = Path(__file__).parent.parent.parent
DOCS_DIR = REPO_ROOT / "docs"

RETIRED_KEYS = {
    "throttle": (
        "ADR-0022 replaced [throttle.*] with [dispatch_pct.*]. Docs reference "
        "it deliberately: superseded ADRs 0015/0016 record it as history and "
        "docs/cheatsheet.md carries the old-to-new migration table. "
        "config.loader._reject_legacy_throttle hard-errors on the key, so an "
        "operator cannot silently resurrect it."
    ),
    "claude.plan": (
        "Never read by any runtime code; removed 2026-09-25. ADR-0022 records "
        "the design that named it, and the cheat sheet's 'Add a new plan' "
        "says it is gone."
    ),
    "plans": "Never read; removed with claude.plan, and documented alongside it.",
    "session.resume_fail_fast_s": (
        "Never read; removed 2026-09-25. ADR-0005 records the fall-through it "
        "was meant to time, and its dated update says that was never built."
    ),
    "ema": (
        "Never wired into dispatch; removed 2026-09-25. ADR-0011 (deprecated) "
        "records the design, and its dated update says the table is gone."
    ),
}
"""Tables and fields that no longer exist but which docs may still name.

A dotted entry covers itself and everything under it: ``"throttle"`` is
the whole ``[throttle.*]`` tree, ``"claude.plan"`` one field. Add an
entry ONLY for a key that docs describe as retired/historical. A key an
operator might still be told to *set* does not belong here -- that is
the bug this test exists to catch. Every entry must also be rejected by
a loader guard that names it (checked below), so an operator who follows
a stale mention gets told to delete the key.
"""


def _is_retired(path: str) -> bool:
    return any(path == key or path.startswith(f"{key}.") for key in RETIRED_KEYS)


def _retired_shown(key: str) -> str:
    """How the loader's messages write a retired key: ``[claude].plan``, ``[plans.*]``."""
    table, _, field = key.rpartition(".")
    return f"[{table}].{field}" if table else f"[{field}.*]"


_PLACEHOLDER = re.compile(r"^(?:\*|<[^>]*>|\.\.\.|N|NNN)$")
"""A doc placeholder segment (``<model>``, ``*``). Unverifiable, so it
terminates matching successfully rather than failing a real path."""

_INLINE_REF = re.compile(r"\[([a-z_][\w.]*)\]\.([a-z_][\w.<>]*)")
"""``[table].field`` written in prose -- the shape of both real bugs."""

_CODE_SPAN = re.compile(r"`([^`\n]+)`")
_SPAN_REF = re.compile(r"^\[\[?([a-z_][\w.<>]*?)(?:\.\*)?\]?\](?:\.([a-z_][\w.<>]*))?$")
"""A whole code span that is a config reference: `[queue]`,
`[hooks].pre_dispatch_command`, `[[accounts]]`, `[dispatch_pct.*]`,
`[dispatch_pct.<band>]`."""

_FENCE = re.compile(r"^```+\s*([a-zA-Z0-9_-]*)\s*$")
_TOML_TABLE = re.compile(r"^\[\[?([a-z_][\w.<>]*)\]?\]$")
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


class _Leaf(BaseModel):
    depth: int


class _ByTwoKeys(BaseModel):
    table: dict[str, dict[str, _Leaf]]


class TestSchemaWalker:
    """The matcher itself -- a broken checker would pass everything."""

    def test_resolves_nested_path(self) -> None:
        assert _is_known("dispatch_pct.day.fivehr_stop_pct")

    def test_resolves_bare_table(self) -> None:
        assert _is_known("queue")

    def test_resolves_through_list_of_models(self) -> None:
        assert _is_known("accounts.config_dir")

    def test_resolves_through_nested_dict_keys(self) -> None:
        # No live table nests two operator-chosen keys since [ema.priors]
        # went, so a stand-in model keeps this branch covered.
        assert _matches(_ByTwoKeys, ("table", "opus", "high", "depth"))
        assert not _matches(_ByTwoKeys, ("table", "opus", "high", "nope"))

    def test_resolves_through_a_dict_of_lists(self) -> None:
        # effort_levels is dict[str, list[str]]: the model name is the key.
        assert _is_known("effort_levels.claude-opus-5-5")

    def test_placeholder_segment_terminates(self) -> None:
        # dispatch_pct is a model, not a dict: only the placeholder rule
        # lets "<band>" through.
        assert _is_known("dispatch_pct.<band>.fivehr_stop_pct")
        assert not _is_known("dispatch_pct.band.fivehr_stop_pct")

    def test_rejects_deleted_table(self) -> None:
        assert not _is_known("sidecar.unanswered_auto_recommended_s")
        assert not _is_known("notify.channels")
        assert not _is_known("plans.max20x.weekly_tokens")
        assert not _is_known("ema.priors.<model>.<effort>")

    def test_rejects_deleted_field_on_real_table(self) -> None:
        assert _is_known("claude.config_dir")
        assert not _is_known("claude.plan")

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

    def test_extracts_a_table_with_placeholder_segments(self) -> None:
        # docs/architecture.md said "edit `[ema.priors.<model>.<effort>]`";
        # until 2026-09-25 this shape was not extracted, so the gate could
        # not have flagged it once [ema] was gone.
        assert _iter_doc_refs("edit `[ema.priors.<model>.<effort>]` per queue") == [
            (1, "ema.priors.<model>.<effort>")
        ]
        block = "```toml\n[plans.<tier>]\nweekly_tokens = 1\n```"
        assert _iter_doc_refs(block) == [(2, "plans.<tier>"), (3, "plans.<tier>.weekly_tokens")]

    def test_extracts_an_array_of_tables(self) -> None:
        assert _iter_doc_refs("one `[[accounts]]` block per login") == [(1, "accounts")]

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
            if _is_retired(path) or _is_known(path):
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
            "for a key docs describe as retired -- add it to "
            "RETIRED_KEYS in this file."
        )


class TestRetiredKeys:
    def test_an_entry_covers_itself_and_what_is_under_it(self) -> None:
        assert _is_retired("throttle")
        assert _is_retired("throttle.five_hour.band_slowdown_max_pct")
        assert _is_retired("claude.plan")
        assert not _is_retired("claude.config_dir")
        assert not _is_retired("claude")
        # A prefix match on whole segments only.
        assert not _is_retired("plansx")
        assert not _is_retired("claude.planned")

    def test_shown_like_the_loader_messages(self) -> None:
        assert _retired_shown("throttle") == "[throttle.*]"
        assert _retired_shown("claude.plan") == "[claude].plan"

    @pytest.mark.parametrize("key", sorted(RETIRED_KEYS))
    def test_the_loader_rejects_it_by_name(self, key: str, tmp_path: Path) -> None:
        # Docs may keep naming a retired key only because an operator who
        # follows a stale mention is told to delete it. A plain
        # extra="forbid" rejection would not name it as [table].field, so
        # this fails unless a dedicated guard fired.
        table, _, field = key.rpartition(".")
        toml = tmp_path / "claude_runner.toml"
        toml.write_text(f"[{table}]\n{field} = 1\n" if table else f"[{field}]\n")
        with pytest.raises(ConfigError, match=re.escape(_retired_shown(key))):
            load_settings(toml)
