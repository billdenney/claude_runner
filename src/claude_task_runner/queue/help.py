"""Operator help for authoring Task YAMLs.

Malformed task YAMLs are a recurring authoring failure: the schema is
``extra="forbid"`` (a typo'd key is rejected) and several fields are not
exposed by ``queue add``, so operators hand-write YAML and guess. This module
turns that around with three operator-facing helpers, all derived from the
:class:`~claude_task_runner.queue.schema.Task` model so they never drift:

* :func:`task_template` — a complete, copy-paste, annotated example YAML,
  *generated* from the model (field list, types, defaults, and descriptions all
  come from ``Task.model_fields``; only the handful of example values and the
  choice of which fields to show uncommented are curated here).
* :func:`field_reference` — the authoritative field table (name / required /
  type / default / description) generated from the model.
* :func:`explain_validation_error` — turns a pydantic ``ValidationError`` into
  actionable lines ("unknown field 'X' (did you mean 'Y'?)", "required field
  'Z' is missing", allowed enum values) plus a pointer to ``queue template``.

The CLI exposes the first two via ``claude-task-runner queue template``; the
third is used by the YAML loader (``queue.store._validate``) so every bad task
file fails with a fixable message.
"""

from __future__ import annotations

import difflib
import typing
from typing import Any, Literal, Union

from pydantic import BaseModel, ValidationError

from .schema import Task

# ---------------------------------------------------------------------------
# Type / default rendering (model -> human-readable)
# ---------------------------------------------------------------------------


def _type_str(ann: Any) -> str:
    """Compact, operator-friendly rendering of a field annotation."""
    origin = typing.get_origin(ann)
    args = typing.get_args(ann)
    if origin is Literal:
        return " | ".join(repr(a) if not isinstance(a, str) else a for a in args)
    if origin in (Union, getattr(__import__("types"), "UnionType", None)):
        inner = [a for a in args if a is not type(None)]
        nullable = type(None) in args
        rendered = " | ".join(_type_str(a) for a in inner)
        return f"{rendered} or null" if nullable else rendered
    if origin in (list, typing.List):  # noqa: UP006
        return f"list[{_type_str(args[0])}]" if args else "list"
    if ann is type(None):
        return "null"
    name = getattr(ann, "__name__", None)
    if name == "Path":
        return "path"
    return name or str(ann).replace("typing.", "")


def _default_str(field: Any) -> str:
    if field.is_required():
        return "(required)"
    # Distinguish a default_factory (e.g. list) from an explicit default.
    if field.default_factory is not None:
        try:
            val = field.default_factory()
        except Exception:  # pragma: no cover - defensive
            val = None
        return repr(val)
    return repr(field.default)


def _clean_desc(desc: str | None) -> str:
    """Collapse whitespace and drop RST ``double-backticks`` for plain text."""
    return " ".join((desc or "").replace("``", "").split())


def _first_sentence(desc: str | None, cap: int = 100) -> str:
    """First sentence of a cleaned description, for a terse inline comment."""
    text = _clean_desc(desc)
    dot = text.find(". ")
    if dot != -1:
        text = text[: dot + 1]
    if len(text) > cap:
        text = text[: cap - 3].rstrip() + "..."
    return text


# ---------------------------------------------------------------------------
# Field reference (authoritative; generated from the model)
# ---------------------------------------------------------------------------


def field_reference() -> str:
    """The authoritative Task-field table, generated from the model."""
    rows = []
    for name, field in Task.model_fields.items():
        req = "required" if field.is_required() else "optional"
        desc = _clean_desc(field.description)
        rows.append((name, req, _type_str(field.annotation), _default_str(field), desc))
    name_w = max(len(r[0]) for r in rows)
    type_w = min(28, max(len(r[2]) for r in rows))
    out = ["Task YAML fields (claude-task-runner queue):", ""]
    for name, req, typ, default, desc in rows:
        out.append(f"  {name:<{name_w}}  {req:<8}  {typ:<{type_w}}  {default}")
        if desc:
            out.append(f"  {'':<{name_w}}  {desc}")
    out.append("")
    out.append(
        "Unknown keys are rejected. Required: "
        + ", ".join(n for n, f in Task.model_fields.items() if f.is_required())
        + "."
    )
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Template (generated from the model)
# ---------------------------------------------------------------------------

# The two curated tables below are the only parts NOT derivable from the model:
#
# * _EXAMPLES — illustrative values. A required field (id/title/prompt) has no
#   default to show, and a default of ``None``/``[]`` makes a poor example, so a
#   concrete value is supplied. Everything else uses the field's own default.
# * _FEATURED — which fields appear UNCOMMENTED, so the emitted YAML is a
#   runnable starting point. Every required field is always featured; these
#   common optional ones are added on top. Pure presentation.
#
# Both are keyed/named by Task field; ``test_queue_help`` asserts every key is a
# real field, so a rename can't leave a stale entry. (They could move onto the
# model as ``Field(examples=...)`` / metadata later; kept local for now.)

_EXAMPLES: dict[str, Any] = {
    "id": "001-author_year_drug",
    "title": "Extract the Author 20xx drug popPK model",
    "prompt": (
        "Use the /extract-literature-model skill to add the population PK model\n"
        "from the paper at /abs/path/to/PMID_XXXX_pmc.xml. Values and equations\n"
        "must come from the source on disk."
    ),
    "working_dir": "/abs/path/to/worktree",
    "allowed_tools": ["Read", "Edit", "Write", "Bash", "Grep", "Glob", "WebFetch"],
    "account": "personal",
    "max_tokens_override": 2000000,
    "max_duration_s_override": 7200,
}

_FEATURED: frozenset[str] = frozenset(
    {
        "schema_version",
        "id",
        "title",
        "prompt",
        "model",
        "effort",
        "priority",
        "allowed_tools",
        "working_dir",
    }
)

_HEADER = [
    "# claude-task-runner Task YAML  --  save as <queue>/todo/<id>.yaml",
    "#",
    "# Generated from the Task model: every field below is real, shown with its",
    "# default and description. Required + common fields are uncommented (a",
    "# runnable starting point); the rest are commented with their default.",
    "# Unknown keys are REJECTED -- field table: `queue template --reference`.",
    "",
]

_COMMENT_COL = 42  # align trailing `# description` comments to this column


def _example_for(name: str, field: Any) -> Any:
    """Best example value for a field: its curated example, else its default."""
    if name in _EXAMPLES:
        return _EXAMPLES[name]
    if not field.is_required():
        if field.default_factory is not None:
            return field.default_factory()
        return field.default
    return "<value>"  # required field with no curated example (shouldn't happen)


def _yaml_scalar(v: Any) -> str:
    """Render a Python value as a single-line YAML scalar/flow value."""
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_yaml_scalar(x) for x in v) + "]"
    s = str(v)
    specials = set(" :#[]{}>|*&!%@,`\"'")
    if s == "" or s.strip() != s or any(c in specials for c in s):
        return '"' + s.replace('"', '\\"') + '"'
    return s


def _emit_field(name: str, field: Any, *, commented: bool) -> list[str]:
    """Render one field as YAML line(s): ``key: value    # description``.

    A multi-line string value becomes a block scalar (``key: |`` + indented
    body); only ever used for an uncommented field (e.g. ``prompt``).
    """
    prefix = "# " if commented else ""
    desc = _first_sentence(field.description)
    value = _example_for(name, field)
    if isinstance(value, str) and "\n" in value:
        head = f"{prefix}{name}: |"
        if desc:
            head = f"{head.ljust(_COMMENT_COL)} # {desc}"
        body = [f"{prefix}  {ln}" for ln in value.rstrip("\n").split("\n")]
        return [head, *body]
    line = f"{prefix}{name}: {_yaml_scalar(value)}"
    if desc:
        line = f"{line.ljust(_COMMENT_COL)} # {desc}"
    return [line]


def task_template() -> str:
    """A complete, annotated, copy-paste Task YAML, generated from the model.

    Featured fields (required + common) are uncommented so the result is a
    runnable starting point; every other field is emitted commented with its
    default, so nothing is hidden and the example always covers the full schema.
    """
    lines = list(_HEADER)
    for name, field in Task.model_fields.items():
        if name in _FEATURED or field.is_required():
            lines += _emit_field(name, field, commented=False)
    lines.append("")
    lines.append("# --- optional (defaults shown; uncomment and edit to set) ---")
    for name, field in Task.model_fields.items():
        if name in _FEATURED or field.is_required():
            continue
        lines += _emit_field(name, field, commented=True)
    return "\n".join(lines) + "\n"


def template_covers_all_fields() -> list[str]:
    """Field names the generated template fails to emit (generator self-check)."""
    tpl = task_template()
    return [n for n in Task.model_fields if f"{n}:" not in tpl]


# ---------------------------------------------------------------------------
# Friendly validation errors
# ---------------------------------------------------------------------------


def explain_validation_error(
    exc: ValidationError, path: Any | None = None, model: type[BaseModel] = Task
) -> str:
    """Render a pydantic ValidationError as actionable authoring guidance."""
    valid = list(model.model_fields)
    lines: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        etype = err.get("type", "")
        if etype == "extra_forbidden":
            near = difflib.get_close_matches(str(loc), valid, n=1)
            hint = f" (did you mean '{near[0]}'?)" if near else ""
            lines.append(f"  unknown field '{loc}'{hint} -- not a valid Task key")
        elif etype == "missing":
            lines.append(f"  required field '{loc}' is missing")
        elif "literal" in etype or "enum" in etype:
            allowed = err.get("ctx", {}).get("expected")
            got = err.get("input")
            allowed_s = f"; allowed: {allowed}" if allowed else ""
            lines.append(f"  field '{loc}': invalid value {got!r}{allowed_s}")
        else:
            lines.append(f"  field '{loc}': {err.get('msg', etype)}")
    head = f"{path}: " if path is not None else ""
    footer = [
        "",
        "Valid Task fields: " + ", ".join(valid) + ".",
    ]
    if model is Task:
        footer.append("For a complete annotated example run: `claude-task-runner queue template`.")
    return head + "invalid task YAML:\n" + "\n".join(lines) + "\n" + "\n".join(footer)
