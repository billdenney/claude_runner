"""Tests for the Task-YAML authoring help (queue template / friendly errors)."""

from __future__ import annotations

import yaml
from pydantic import ValidationError

from claude_task_runner.queue.help import (
    explain_validation_error,
    field_reference,
    task_template,
    template_covers_all_fields,
)
from claude_task_runner.queue.schema import Task


def test_template_covers_every_field():
    # Drift guard: adding a Task field without updating the template fails here.
    assert template_covers_all_fields() == []


def test_template_is_itself_a_valid_task():
    data = yaml.safe_load(task_template())
    task = Task.model_validate(data)
    assert task.id and task.title and task.prompt


def test_field_reference_lists_all_fields_with_descriptions():
    ref = field_reference()
    for name in Task.model_fields:
        assert name in ref, f"{name} missing from field reference"
    # description text is lifted from the model's attribute docstrings
    assert "filename stem" in ref  # from id's docstring


def test_explain_unknown_field_suggests_closest_and_points_to_template():
    msg = ""
    try:
        Task.model_validate(
            {"schema_version": 2, "id": "x", "title": "t", "prompt": "p", "idd": "y"}
        )
    except ValidationError as exc:
        msg = explain_validation_error(exc, "todo/x.yaml")
    assert "unknown field 'idd'" in msg
    assert "did you mean 'id'" in msg
    assert "queue template" in msg


def test_explain_missing_required_and_bad_enum():
    msg = ""
    try:
        Task.model_validate({"schema_version": 2, "title": "t", "priority": "urgent"})
    except ValidationError as exc:
        msg = explain_validation_error(exc)
    assert "required field 'id' is missing" in msg
    assert "required field 'prompt' is missing" in msg
    assert "invalid value 'urgent'" in msg
    assert "low" in msg and "high" in msg
