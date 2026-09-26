"""The selector's effort gate (ADR-0010).

``Task.effort`` is a free-form string. The task schema cannot check it: the
accepted set for each model is the merged settings' ``[effort_levels]``,
which ``load_task`` never sees. So the supervisor's candidate selector checks
each task's (model, effort) pair against the settings it runs with. A task
whose pair fails is parked as ``deferred`` with an ``invalid effort: ...``
reason instead of being dispatched, and is un-parked the first tick after the
task YAML or ``[effort_levels]`` is fixed. ``_dispatch_one_safely`` re-checks
the pair as the backstop for every path that spawns it.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from claude_task_runner.clock import RealClock
from claude_task_runner.config.loader import load_defaults
from claude_task_runner.queue.schema import (
    RunRecord,
    SidecarAnswer,
    SidecarQuestion,
    SidecarRequest,
    SidecarResponse,
    Task,
    TaskState,
)
from claude_task_runner.queue.sidecar import write_response
from claude_task_runner.queue.store import (
    load_state,
    queue_runtime_dir,
    state_path_for,
    task_path_for,
    todo_dir,
    write_state_atomic,
    write_task_atomic,
)
from claude_task_runner.runner import effort_levels, readiness
from claude_task_runner.runner import orchestrator as orchestrator_mod
from claude_task_runner.runner.orchestrator import _dispatch_one_safely, _eligible_candidates

from ._sidecar_files import write_request

DEFAULT_LEVELS: dict[str, list[str]] = load_defaults()["effort_levels"]
"""The package-default ``[effort_levels]``: what a queue without its own
table runs with."""

SONNET_MAX_REASON = (
    "invalid effort: effort 'max' not in accepted set for model "
    "'claude-sonnet-4-6': ['high', 'low', 'medium']"
)
UNKNOWN_MODEL_REASON = (
    "invalid effort: model 'claude-newmodel-99' has no effort levels configured; "
    'add "claude-newmodel-99" = [<levels>] under [effort_levels] in '
    "claude_runner.toml or use a configured model"
)
PARK_WARNING_SUFFIX = (
    ". Fix the task's model or effort, or add the pair to [effort_levels] and "
    "send the supervisor SIGHUP"
)
ORCHESTRATOR_LOGGER = "claude_task_runner.runner.orchestrator"

_NOW = dt.datetime(2026, 9, 26, 12, 0, 0, tzinfo=dt.UTC)


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    return qd


def _write_task(qd: Path, task_id: str, **overrides: Any) -> Task:
    payload: dict[str, Any] = {"id": task_id, "title": f"Task {task_id}", "prompt": "do it"}
    payload.update(overrides)
    task = Task.model_validate(payload)
    write_task_atomic(task, task_path_for(qd, task_id))
    return task


def _write_state(qd: Path, state: TaskState) -> TaskState:
    write_state_atomic(state, state_path_for(qd, state.task_id))
    return state


def _state(qd: Path, task_id: str) -> TaskState:
    return load_state(state_path_for(qd, task_id))


def _candidate_ids(
    qd: Path,
    *,
    levels: dict[str, list[str]] | None = None,
    completed: frozenset[str] = frozenset(),
) -> set[str]:
    eligible = _eligible_candidates(
        qd,
        {},
        set(completed),
        now=_NOW,
        effort_levels=DEFAULT_LEVELS if levels is None else levels,
    )
    return {t.id for t in eligible}


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == ORCHESTRATOR_LOGGER and r.levelno == logging.WARNING
    ]


def _raise_oserror(msg: str) -> Callable[..., None]:
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError(msg)

    return _boom


# ---------------------------------------------------------------------------
# Parking
# ---------------------------------------------------------------------------


class TestPark:
    def test_task_defaults_are_accepted_and_write_no_state(self, queue_dir: Path) -> None:
        """A task YAML that leaves model and effort out is dispatchable, and
        the gate leaves no trace on a task it passes."""
        _write_task(queue_dir, "t-ok")
        assert _candidate_ids(queue_dir) == {"t-ok"}
        assert not state_path_for(queue_dir, "t-ok").exists()

    def test_effort_the_model_does_not_accept_is_parked(self, queue_dir: Path) -> None:
        _write_task(queue_dir, "t-bad", model="claude-sonnet-4-6", effort="max")

        assert _candidate_ids(queue_dir) == set()

        state = _state(queue_dir, "t-bad")
        assert state.status == "deferred"
        assert state.deferred_reason == SONNET_MAX_REASON
        assert effort_levels.is_hold_reason(state.deferred_reason)
        # No cooldown: the gate itself is what holds the task, so a fix is
        # picked up on the very next tick.
        assert state.next_eligible_at is None

    def test_model_missing_from_effort_levels_is_parked(self, queue_dir: Path) -> None:
        _write_task(queue_dir, "t-new", model="claude-newmodel-99", effort="high")

        assert _candidate_ids(queue_dir) == set()

        state = _state(queue_dir, "t-new")
        assert (state.status, state.deferred_reason) == ("deferred", UNKNOWN_MODEL_REASON)

    def test_effort_is_case_sensitive(self, queue_dir: Path) -> None:
        """No normalisation: ``MAX`` is not ``max``, and a pair the CLI would
        not recognise is not waved through."""
        _write_task(queue_dir, "t-caps", model="claude-opus-5-5", effort="MAX")
        assert _candidate_ids(queue_dir) == set()
        assert _state(queue_dir, "t-caps").status == "deferred"

    def test_one_bad_task_does_not_hold_the_rest(self, queue_dir: Path) -> None:
        _write_task(queue_dir, "t-bad", model="claude-sonnet-4-6", effort="max")
        _write_task(queue_dir, "t-ok")
        assert _candidate_ids(queue_dir) == {"t-ok"}

    def test_park_is_not_an_attempt(self, queue_dir: Path) -> None:
        """``attempts`` and ``runs`` are untouched, so a task parked for a
        config error never drifts toward the circuit breaker."""
        _write_task(queue_dir, "t-bad", model="claude-sonnet-4-6", effort="max")
        run = RunRecord(
            attempt=1,
            started_at=_NOW,
            finished_at=_NOW,
            stop_reason="process_exit_nonzero",
            duration_s=1.0,
        )
        _write_state(queue_dir, TaskState(task_id="t-bad", status="failed", attempts=1, runs=[run]))

        _candidate_ids(queue_dir)

        state = _state(queue_dir, "t-bad")
        assert state.status == "deferred"
        assert (state.attempts, state.runs) == (1, [run])

    def test_warns_once_and_writes_once(
        self, queue_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """One WARNING and one write when the task is parked, then nothing
        while it stays parked: the selector runs every tick over every task."""
        _write_task(queue_dir, "t-bad", model="claude-sonnet-4-6", effort="max")
        sp = state_path_for(queue_dir, "t-bad")

        with caplog.at_level(logging.WARNING, logger=ORCHESTRATOR_LOGGER):
            _candidate_ids(queue_dir)
            first_mtime = sp.stat().st_mtime_ns
            for _ in range(3):
                _candidate_ids(queue_dir)

        assert _warnings(caplog) == [
            "parking task t-bad as deferred: " + SONNET_MAX_REASON + PARK_WARNING_SUFFIX
        ]
        assert sp.stat().st_mtime_ns == first_mtime

    def test_reason_is_rewritten_when_the_error_changes(self, queue_dir: Path) -> None:
        """A partial fix (the model added, but without this effort) replaces
        the reason, so the state always names the error that holds the task."""
        _write_task(queue_dir, "t-new", model="claude-newmodel-99", effort="max")
        _candidate_ids(queue_dir)

        _candidate_ids(queue_dir, levels={**DEFAULT_LEVELS, "claude-newmodel-99": ["low"]})

        state = _state(queue_dir, "t-new")
        assert state.status == "deferred"
        assert state.deferred_reason == (
            "invalid effort: effort 'max' not in accepted set for model "
            "'claude-newmodel-99': ['low']"
        )


# ---------------------------------------------------------------------------
# The package defaults
# ---------------------------------------------------------------------------


class TestPackageDefaults:
    @pytest.mark.parametrize(
        ("model", "effort"),
        [
            ("claude-opus-4-7", "max"),
            ("claude-opus-4-7", "xhigh"),
            ("claude-sonnet-4-6", "high"),
        ],
    )
    def test_previous_generation_pairs_stay_dispatchable(
        self, queue_dir: Path, model: str, effort: str
    ) -> None:
        """config/defaults/settings.toml keeps the previous-generation models
        so tasks queued before the default model moved on still run. Dropping
        one would park every queued task that names it; this fails first."""
        _write_task(queue_dir, "t-old", model=model, effort=effort)
        assert _candidate_ids(queue_dir) == {"t-old"}

    def test_a_queued_task_with_the_old_spelling_dispatches_as_xhigh(self, queue_dir: Path) -> None:
        """A task YAML written before ``extra_high`` became ``xhigh`` is read
        with the new name, so it passes the gate and claude gets a level it
        knows, instead of ignoring the flag and running at its default."""
        path = task_path_for(queue_dir, "t-old")
        path.write_text(
            "id: t-old\ntitle: T\nprompt: p\nmodel: claude-opus-5-5\neffort: extra_high\n",
            encoding="utf-8",
        )
        [task] = _eligible_candidates(queue_dir, {}, set(), now=_NOW, effort_levels=DEFAULT_LEVELS)
        assert (task.id, task.effort) == ("t-old", "xhigh")

    def test_every_configured_pair_is_dispatchable(self, queue_dir: Path) -> None:
        """Enumerates the default table: the gate accepts every pair it lists."""
        expected = set()
        for model, levels in DEFAULT_LEVELS.items():
            for effort in levels:
                task_id = f"t-{model}-{effort}"
                _write_task(queue_dir, task_id, model=model, effort=effort)
                expected.add(task_id)

        assert _candidate_ids(queue_dir) == expected
        assert list(queue_dir.glob(".claude_task_runner/state/*.yaml")) == []


# ---------------------------------------------------------------------------
# Un-parking
# ---------------------------------------------------------------------------


class TestUnpark:
    def test_fixing_the_task_yaml_unparks_it(
        self, queue_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write_task(queue_dir, "t-bad", model="claude-sonnet-4-6", effort="max")
        _candidate_ids(queue_dir)
        _write_task(queue_dir, "t-bad", model="claude-sonnet-4-6", effort="high")

        with caplog.at_level(logging.INFO, logger=ORCHESTRATOR_LOGGER):
            assert _candidate_ids(queue_dir) == {"t-bad"}

        state = _state(queue_dir, "t-bad")
        assert (state.status, state.deferred_reason, state.next_eligible_at) == (
            "pending",
            None,
            None,
        )
        assert [r.getMessage() for r in caplog.records if r.levelno == logging.INFO] == [
            "task t-bad: un-parked, its hold has cleared: " + SONNET_MAX_REASON
        ]

    def test_adding_the_model_to_effort_levels_unparks_it(self, queue_dir: Path) -> None:
        """The other fix: the queue's TOML gains the model, and the supervisor
        re-reads it (SIGHUP) into the settings the selector is handed."""
        _write_task(queue_dir, "t-new", model="claude-newmodel-99", effort="high")
        _candidate_ids(queue_dir)
        assert _state(queue_dir, "t-new").status == "deferred"

        levels = {**DEFAULT_LEVELS, "claude-newmodel-99": ["low", "medium", "high"]}
        assert _candidate_ids(queue_dir, levels=levels) == {"t-new"}
        assert _state(queue_dir, "t-new").status == "pending"

    def test_never_clears_a_readiness_hold(self, queue_dir: Path) -> None:
        """The effort gate un-parks only its own holds. A task with a valid
        pair held by the readiness gate keeps that hold, untouched: had the
        effort gate cleared it, the readiness gate would have re-written it."""
        _write_task(queue_dir, "t-held", requires=[{"kind": "file", "path": "inputs/missing.md"}])
        _candidate_ids(queue_dir)
        sp = state_path_for(queue_dir, "t-held")
        held = _state(queue_dir, "t-held")
        assert readiness.is_hold_reason(held.deferred_reason)
        first_mtime = sp.stat().st_mtime_ns

        assert _candidate_ids(queue_dir) == set()

        assert _state(queue_dir, "t-held") == held
        assert sp.stat().st_mtime_ns == first_mtime

    def test_never_clears_an_operator_park(self, queue_dir: Path) -> None:
        """An operator's park whose cooldown has run out falls through to the
        gates like a hook deferral does. The effort gate must not rewrite it:
        its reason is the operator's, not ``invalid effort: ...``."""
        _write_task(queue_dir, "t-parked")
        parked = _write_state(
            queue_dir,
            TaskState(
                task_id="t-parked",
                status="deferred",
                deferred_reason="PARKED 2026-09-01: waiting on a supplement",
                next_eligible_at=_NOW - dt.timedelta(minutes=1),
            ),
        )

        assert _candidate_ids(queue_dir) == {"t-parked"}
        assert _state(queue_dir, "t-parked") == parked


# ---------------------------------------------------------------------------
# Which tasks the gate applies to
# ---------------------------------------------------------------------------


def _request(task_id: str) -> SidecarRequest:
    return SidecarRequest(
        task_id=task_id,
        sequence=1,
        created_at=_NOW,
        summary="placeholder",
        context="placeholder",
        questions=[SidecarQuestion(id="q1", prompt="proceed?")],
    )


def _response(task_id: str) -> SidecarResponse:
    return SidecarResponse(
        task_id=task_id,
        sequence=1,
        responded_at=_NOW,
        answers=[SidecarAnswer(id="q1", value="yes")],
    )


class TestScope:
    @pytest.mark.parametrize(
        "status",
        ["running", "completed", "failed_circuit_breaker", "possibly_hung", "weekly_paused"],
    )
    def test_non_dispatchable_states_are_left_alone(self, queue_dir: Path, status: str) -> None:
        """[effort_levels] changing under a finished, running or given-up task
        must not rewrite its state: it is not about to be dispatched."""
        _write_task(queue_dir, "t", model="claude-sonnet-4-6", effort="max")
        before = _write_state(queue_dir, TaskState(task_id="t", status=status, attempts=1))

        assert _candidate_ids(queue_dir) == set()
        assert _state(queue_dir, "t") == before

    @pytest.mark.parametrize("status", ["no-state", "pending", "failed", "deferred"])
    def test_every_resume_status_is_parked(self, queue_dir: Path, status: str) -> None:
        """Every status the selector would dispatch from is gated, including
        a pre-dispatch hook's deferral whose cooldown has run out."""
        _write_task(queue_dir, "t", model="claude-sonnet-4-6", effort="max")
        if status == "deferred":
            _write_state(
                queue_dir,
                TaskState(
                    task_id="t",
                    status="deferred",
                    deferral_count=1,
                    deferred_reason="DEFERRED: input awaits re-acquisition",
                    next_eligible_at=_NOW - dt.timedelta(minutes=1),
                ),
            )
        elif status != "no-state":
            _write_state(queue_dir, TaskState(task_id="t", status=status, attempts=1))

        assert _candidate_ids(queue_dir) == set()

        state = _state(queue_dir, "t")
        assert (state.status, state.deferred_reason) == ("deferred", SONNET_MAX_REASON)

    def test_answered_sidecar_is_parked(self, queue_dir: Path) -> None:
        """An awaiting_sidecar task whose request is answered would be
        re-dispatched; the gate holds it like any other resume path."""
        _write_task(queue_dir, "t", model="claude-sonnet-4-6", effort="max")
        _write_state(queue_dir, TaskState(task_id="t", status="awaiting_sidecar", attempts=1))
        write_request(queue_dir, _request("t"))
        write_response(queue_dir, _response("t"))

        assert _candidate_ids(queue_dir) == set()
        assert _state(queue_dir, "t").deferred_reason == SONNET_MAX_REASON

    def test_parked_before_its_dependencies_finish(self, queue_dir: Path) -> None:
        """An authoring error surfaces as soon as the task is queued, not
        hours later once ``depends_on`` is met."""
        _write_task(
            queue_dir, "t", model="claude-sonnet-4-6", effort="max", depends_on=["upstream"]
        )

        assert _candidate_ids(queue_dir) == set()
        assert _state(queue_dir, "t").deferred_reason == SONNET_MAX_REASON

    def test_effort_hold_comes_before_a_readiness_hold(self, queue_dir: Path) -> None:
        """Both gates hold the task; the state names the effort error first,
        then the readiness hold once the effort is fixed."""
        requires = [{"kind": "file", "path": "inputs/missing.md"}]
        _write_task(queue_dir, "t", model="claude-sonnet-4-6", effort="max", requires=requires)
        _candidate_ids(queue_dir)
        assert _state(queue_dir, "t").deferred_reason == SONNET_MAX_REASON

        _write_task(queue_dir, "t", model="claude-sonnet-4-6", effort="high", requires=requires)
        assert _candidate_ids(queue_dir) == set()

        state = _state(queue_dir, "t")
        assert state.status == "deferred"
        assert readiness.is_hold_reason(state.deferred_reason)


# ---------------------------------------------------------------------------
# Bookkeeping failures
# ---------------------------------------------------------------------------


class TestWriteFailures:
    def test_failed_park_write_still_skips_the_task_and_warns(
        self,
        queue_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Parking is visibility, not correctness: the task is skipped whether
        or not the state write lands, and the WARNING still names the error."""
        _write_task(queue_dir, "t-bad", model="claude-sonnet-4-6", effort="max")
        _write_task(queue_dir, "t-ok")
        monkeypatch.setattr(
            orchestrator_mod, "write_state_atomic", _raise_oserror("state dir read-only")
        )

        with caplog.at_level(logging.WARNING, logger=ORCHESTRATOR_LOGGER):
            assert _candidate_ids(queue_dir) == {"t-ok"}

        assert _warnings(caplog) == [
            "parking task t-bad as deferred: " + SONNET_MAX_REASON + PARK_WARNING_SUFFIX,
            f"could not park task t-bad as deferred ({SONNET_MAX_REASON}): state dir read-only",
        ]

    def test_failed_unpark_write_still_admits_the_fixed_task(
        self, queue_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_task(queue_dir, "t")
        _write_state(
            queue_dir,
            TaskState(task_id="t", status="deferred", deferred_reason=SONNET_MAX_REASON),
        )
        monkeypatch.setattr(
            orchestrator_mod, "write_state_atomic", _raise_oserror("state dir read-only")
        )

        assert _candidate_ids(queue_dir) == {"t"}


# ---------------------------------------------------------------------------
# _dispatch_one_safely: the backstop
# ---------------------------------------------------------------------------


def _settings(levels: dict[str, list[str]]) -> Any:
    """Just what ``_dispatch_one_safely`` reads before it calls dispatch."""
    return SimpleNamespace(
        effort_levels=levels,
        session=SimpleNamespace(),
        task_caps=SimpleNamespace(),
        hooks=SimpleNamespace(),
        failure_classifier=None,
        dispatch=SimpleNamespace(auto_detect_paths_in_prompt=False),
    )


def _run_backstop(task: Task, qd: Path, levels: dict[str, list[str]]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    with patch(
        "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
        side_effect=lambda **kw: calls.append(kw),
    ):
        _dispatch_one_safely(
            task, qd, _settings(levels), RealClock(), "claude", "", None, "default"
        )
    return calls


class TestDispatchBackstop:
    def test_refuses_to_spawn_and_parks_the_task(
        self, queue_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        task = _write_task(queue_dir, "t-bad", model="claude-sonnet-4-6", effort="max")

        with caplog.at_level(logging.WARNING, logger=ORCHESTRATOR_LOGGER):
            calls = _run_backstop(task, queue_dir, DEFAULT_LEVELS)

        assert calls == []
        assert _warnings(caplog) == ["refusing to dispatch task t-bad: " + SONNET_MAX_REASON]
        state = _state(queue_dir, "t-bad")
        assert (state.status, state.deferred_reason) == ("deferred", SONNET_MAX_REASON)

    def test_checks_the_settings_it_is_handed(self, queue_dir: Path) -> None:
        """A pair only the queue's own [effort_levels] accepts dispatches: the
        check uses the settings the supervisor runs with, not the defaults."""
        task = _write_task(queue_dir, "t-custom", model="claude-custom-1", effort="low")

        calls = _run_backstop(task, queue_dir, {"claude-custom-1": ["low"]})

        assert len(calls) == 1
        assert calls[0]["task"].id == "t-custom"
