"""Operator help for authoring Task YAMLs.

Malformed task YAMLs are a recurring authoring failure: the schema is
``extra="forbid"`` (a typo'd key is rejected) and several fields are not
exposed by ``queue add``, so operators hand-write YAML and guess. This module
turns that around with three operator-facing helpers, all derived from the
:class:`~claude_task_runner.queue.schema.Task` model so they never drift:

* :func:`task_template` — a complete, copy-paste, annotated example YAML.
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

from pydantic import ValidationError

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


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def field_reference() -> str:
    """The authoritative Task-field table, generated from the model."""
    rows = []
    for name, field in Task.model_fields.items():
        req = "required" if field.is_required() else "optional"
        desc = " ".join((field.description or "").split())
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


_TEMPLATE = """\
# claude-task-runner Task YAML  -- save as <queue>/todo/<id>.yaml
#
# Required keys are uncommented; optional keys are shown commented with their
# default. Unknown keys are REJECTED (the schema forbids extras), so do not
# invent fields. Authoritative field list: `claude-task-runner queue template --reference`.

schema_version: 2                       # required, always 2
id: 001-author_year_drug                # required; unique, == filename stem
title: "Extract the Author 20xx drug popPK model"   # required
prompt: |                               # required; block scalar keeps quoting safe
  Use the /extract-literature-model skill to add the population PK model from
  the paper at /abs/path/to/PMID_XXXX_pmc.xml. Values and equations must come
  from the source on disk.

model: claude-opus-4-7                   # default; a model id the runner config knows
effort: high                            # validated per model (e.g. low | medium | high)
priority: normal                        # low | normal | high  (dispatch ordering)
allowed_tools: [Read, Edit, Write, Bash, Grep, Glob, WebFetch]
working_dir: /abs/path/to/worktree      # agent cwd ($TASK_WORKING_DIR); null = use template

# --- optional scheduling / throttle controls ---
# weekly_critical: false                # true = dispatch first within the weekly window
# weekly_deferrable: false              # true = OK to skip to next week; deprioritized in EOW push
# force_dispatch_in_eow: false          # true = bypass the end-of-week runtime-safety throttle
#                                       #   (to force one task now you can instead run:
#                                       #    claude-task-runner queue force-dispatch <id>)

# --- optional dependencies / routing ---
# depends_on: []                        # task ids that must finish first
# tags: []                              # free-form cohort labels
# account: personal                     # pin to a [[accounts]] name; unset = auto-pick
# additional_dirs: []                   # extra absolute dirs the agent may read/write

# --- optional per-task cap overrides ---
# max_tokens_override: 2000000          # overrides [task_caps].max_tokens_per_task
# max_duration_s_override: 7200         # overrides [task_caps].max_duration_s_per_task
# deliverable_paths: []                 # files the task must produce (output-evidence gate)
"""


def task_template() -> str:
    """A complete, annotated, copy-paste Task YAML covering every field."""
    return _TEMPLATE


def template_covers_all_fields() -> list[str]:
    """Field names absent from the template (drift guard for the test suite)."""
    return [n for n in Task.model_fields if f"{n}:" not in _TEMPLATE]


def explain_validation_error(
    exc: ValidationError, path: Any | None = None, model: type = Task
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
