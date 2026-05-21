"""Tests for priority-based dispatch ordering.

The orchestrator sorts the per-tick dispatch candidates by
``(priority_rank, task_id)`` with ``high=0, normal=1, low=2``. The
ranking lives in :func:`runner.orchestrator.priority_sort_key` and is
consumed by :func:`runner.orchestrator.tick_dispatch` AND the CLI's
``queue list --order-by-dispatch`` flag. This file is the single
source of truth for "is priority handled correctly?"; if the operator
ever needs to re-confirm the ordering rules, every relevant test is
here.

Coverage matrix:

* The sort key function itself — every documented input, including
  the unknown-priority fallback that the static ``Literal`` schema
  protects against at parse time but the runtime code still defends.
* ``planned_dispatch_order`` — the helper the CLI calls — across an
  empty queue, a queue with all-same priorities, a three-band queue,
  and a queue with one unparseable YAML mixed in.
* ``tick_dispatch`` — the supervisor's actual ordering decision —
  across enough scenarios to catch regressions in either the sort
  call site or the eligibility filter that feeds it.
* The ``queue list --order-by-dispatch`` CLI flag — both JSON and
  human-readable output, with three-band + tie-break + empty cases.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from claude_task_runner.clock import RealClock
from claude_task_runner.queue.schema import Task
from claude_task_runner.queue.store import (
    queue_runtime_dir,
    task_path_for,
    todo_dir,
    write_task_atomic,
)
from claude_task_runner.runner.orchestrator import (
    planned_dispatch_order,
    priority_sort_key,
    tick_dispatch,
)
from claude_task_runner.supervisor.states import SupervisorSnapshot, SupervisorState


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    return qd


def _make_task(qd: Path, task_id: str, **overrides: Any) -> Task:
    payload: dict[str, Any] = {
        "id": task_id,
        "title": f"Task {task_id}",
        "prompt": "do the thing",
    }
    payload.update(overrides)
    task = Task.model_validate(payload)
    write_task_atomic(task, task_path_for(qd, task_id))
    return task


def _make_settings(*, initial: int = 1, max_c: int = 5) -> Any:
    """Minimal Settings shape used by tick_dispatch."""
    return SimpleNamespace(
        concurrency=SimpleNamespace(
            initial_concurrency=initial,
            max_concurrency=max_c,
        ),
        task_caps=SimpleNamespace(),
        session=SimpleNamespace(),
        hooks=SimpleNamespace(),
        failure_classifier=None,
        claude=SimpleNamespace(config_dir=""),
    )


def _make_snapshot(state: SupervisorState) -> SupervisorSnapshot:
    return SupervisorSnapshot.model_validate(
        {
            "state": state,
            "since": datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC),
        }
    )


def _capture_dispatch_order(*, queue_dir: Path, settings: Any) -> list[str]:
    """Drive tick_dispatch once and return ``[task_id, ...]`` in dispatch order.

    Wraps ``Thread.start`` so we record the order in which dispatch
    threads were SPAWNED (which is the order ``tick_dispatch`` chose),
    not the order they happened to finish. Patches the dispatcher
    itself to a no-op so the test stays unit-scale.
    """
    snap = _make_snapshot(SupervisorState.DISPATCHING)
    in_flight: dict[str, threading.Thread] = {}
    dispatch_order: list[str] = []
    real_thread_start = threading.Thread.start

    def record_then_start(self: threading.Thread) -> None:
        if self.name.startswith("dispatch-"):
            dispatch_order.append(self.name.removeprefix("dispatch-"))
        real_thread_start(self)

    with (
        patch(
            "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
            return_value=None,
        ),
        patch.object(threading.Thread, "start", record_then_start),
    ):
        tick_dispatch(
            queue_dir=queue_dir,
            settings=settings,
            clock=RealClock(),
            snapshot=snap,
            in_flight_threads=in_flight,
        )
        for th in list(in_flight.values()):
            th.join(timeout=2)
    return dispatch_order


# ---------------------------------------------------------------------------
# priority_sort_key — the sort function itself.
# ---------------------------------------------------------------------------


class TestPrioritySortKey:
    def test_high_ranks_zero(self) -> None:
        t = _make_task_in_memory("any", priority="high")
        assert priority_sort_key(t) == (0, "any")

    def test_normal_ranks_one(self) -> None:
        t = _make_task_in_memory("any", priority="normal")
        assert priority_sort_key(t) == (1, "any")

    def test_low_ranks_two(self) -> None:
        t = _make_task_in_memory("any", priority="low")
        assert priority_sort_key(t) == (2, "any")

    def test_default_priority_is_normal(self) -> None:
        """Tasks without an explicit ``priority`` default to ``normal`` per
        the schema; the sort key must reflect that without special-casing."""
        t = _make_task_in_memory("any")  # no priority kw
        assert priority_sort_key(t) == (1, "any")

    def test_unknown_priority_sinks_to_99(self) -> None:
        """The schema's ``Literal`` accepts only low/normal/high at parse
        time, but the runtime code defends against unknown values with
        the rank-99 fallback. We synthesize the unknown by bypassing
        pydantic validation via ``model_construct``."""
        t = Task.model_construct(
            id="any",
            title="x",
            prompt="x",
            priority="urgent",  # not in the Literal
        )
        assert priority_sort_key(t) == (99, "any")

    def test_sort_key_is_lexicographic_within_band(self) -> None:
        """Sorting a list of tasks using ``priority_sort_key`` orders by
        band, then by id ascending within the band."""
        tasks = [
            _make_task_in_memory("z-high", priority="high"),
            _make_task_in_memory("a-low", priority="low"),
            _make_task_in_memory("m-normal", priority="normal"),
            _make_task_in_memory("a-high", priority="high"),
            _make_task_in_memory("a-normal", priority="normal"),
        ]
        tasks.sort(key=priority_sort_key)
        assert [t.id for t in tasks] == [
            "a-high",
            "z-high",
            "a-normal",
            "m-normal",
            "a-low",
        ]


def _make_task_in_memory(task_id: str, **overrides: Any) -> Task:
    """Build a Task without touching disk — used by the sort-key tests."""
    payload: dict[str, Any] = {
        "id": task_id,
        "title": f"Task {task_id}",
        "prompt": "do the thing",
    }
    payload.update(overrides)
    return Task.model_validate(payload)


# ---------------------------------------------------------------------------
# planned_dispatch_order — used by ``queue list --order-by-dispatch``.
# ---------------------------------------------------------------------------


class TestPlannedDispatchOrder:
    def test_empty_queue_returns_empty_list(self, queue_dir: Path) -> None:
        assert planned_dispatch_order(queue_dir) == []

    def test_single_task_returned_as_is(self, queue_dir: Path) -> None:
        _make_task(queue_dir, "only", priority="normal")
        out = planned_dispatch_order(queue_dir)
        assert [t.id for t in out] == ["only"]

    def test_all_same_priority_orders_by_id(self, queue_dir: Path) -> None:
        _make_task(queue_dir, "c", priority="normal")
        _make_task(queue_dir, "a", priority="normal")
        _make_task(queue_dir, "b", priority="normal")
        out = planned_dispatch_order(queue_dir)
        assert [t.id for t in out] == ["a", "b", "c"]

    def test_three_band_full_order(self, queue_dir: Path) -> None:
        """All three priority bands present: order is high → normal → low,
        with id-ascending tie-break inside each band."""
        _make_task(queue_dir, "n-mid", priority="normal")
        _make_task(queue_dir, "l-1", priority="low")
        _make_task(queue_dir, "h-2", priority="high")
        _make_task(queue_dir, "n-aaa", priority="normal")
        _make_task(queue_dir, "h-1", priority="high")
        _make_task(queue_dir, "l-2", priority="low")
        out = planned_dispatch_order(queue_dir)
        assert [t.id for t in out] == ["h-1", "h-2", "n-aaa", "n-mid", "l-1", "l-2"]

    def test_skips_unparseable_yaml_with_warning(
        self, queue_dir: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An invalid YAML in ``todo/`` must not crash the planner; it
        logs and continues so the operator can still see the rest of
        the queue's dispatch order."""
        _make_task(queue_dir, "good-high", priority="high")
        bad = todo_dir(queue_dir) / "bad.yaml"
        bad.write_text("not even close: ][", encoding="utf-8")
        _make_task(queue_dir, "good-low", priority="low")

        with caplog.at_level("WARNING", logger="claude_task_runner.runner.orchestrator"):
            out = planned_dispatch_order(queue_dir)
        assert [t.id for t in out] == ["good-high", "good-low"]
        assert any("unparseable" in r.message for r in caplog.records)

    def test_matches_tick_dispatch_order_for_same_inputs(self, queue_dir: Path) -> None:
        """The supervisor's ``tick_dispatch`` ordering and the CLI's
        ``planned_dispatch_order`` must agree — the operator uses the
        latter to verify the former, so any drift would silently mislead."""
        # Three bands + tie-breaks to exercise the full sort.
        _make_task(queue_dir, "h-bbb", priority="high")
        _make_task(queue_dir, "l-aaa", priority="low")
        _make_task(queue_dir, "n-zzz", priority="normal")
        _make_task(queue_dir, "h-aaa", priority="high")
        _make_task(queue_dir, "n-aaa", priority="normal")

        planned = [t.id for t in planned_dispatch_order(queue_dir)]
        # Big enough max_c that every task gets a slot — so the dispatch
        # order is observable, not truncated.
        settings = _make_settings(initial=10, max_c=10)
        actual = _capture_dispatch_order(queue_dir=queue_dir, settings=settings)
        assert planned == actual


# ---------------------------------------------------------------------------
# tick_dispatch — supervisor's actual choice across scenarios.
# ---------------------------------------------------------------------------


class TestTickDispatchPriorityScenarios:
    def test_three_bands_one_slot_high_wins(self, queue_dir: Path) -> None:
        """One slot free, one task in each band: high dispatches, others wait."""
        _make_task(queue_dir, "h", priority="high")
        _make_task(queue_dir, "n", priority="normal")
        _make_task(queue_dir, "l", priority="low")
        settings = _make_settings(initial=1, max_c=1)
        order = _capture_dispatch_order(queue_dir=queue_dir, settings=settings)
        assert order == ["h"]

    def test_three_bands_two_slots_picks_high_then_normal(self, queue_dir: Path) -> None:
        """Two slots: high then normal. Low waits."""
        _make_task(queue_dir, "h", priority="high")
        _make_task(queue_dir, "n", priority="normal")
        _make_task(queue_dir, "l", priority="low")
        settings = _make_settings(initial=2, max_c=2)
        order = _capture_dispatch_order(queue_dir=queue_dir, settings=settings)
        assert order == ["h", "n"]

    def test_two_low_one_normal_normal_first(self, queue_dir: Path) -> None:
        """A single ``normal`` task dispatches before any ``low``,
        regardless of filename order. The original bug report was about
        high-vs-normal; this test pins the same rule for normal-vs-low
        so a future refactor can't silently invert the bottom of the
        ranking."""
        _make_task(queue_dir, "001-low", priority="low")
        _make_task(queue_dir, "002-low", priority="low")
        _make_task(queue_dir, "999-normal", priority="normal")
        settings = _make_settings(initial=1, max_c=1)
        order = _capture_dispatch_order(queue_dir=queue_dir, settings=settings)
        assert order == ["999-normal"]

    def test_high_priority_late_yaml_jumps_ahead(self, queue_dir: Path) -> None:
        """Regression: this is the operator's original observation.
        A ``priority: high`` task whose YAML filename comes LAST
        alphabetically must dispatch FIRST — exactly what the operator
        expected of 130-lowe_2009_omalizumab on 2026-05-20 (Lowe was
        filename-late but priority-high)."""
        for i in range(1, 130):
            _make_task(queue_dir, f"{i:03d}-noise", priority="normal")
        _make_task(queue_dir, "130-lowe", priority="high")
        settings = _make_settings(initial=1, max_c=1)
        order = _capture_dispatch_order(queue_dir=queue_dir, settings=settings)
        assert order == ["130-lowe"]

    def test_all_same_band_ordered_by_id(self, queue_dir: Path) -> None:
        """Within a band, ties break by id ascending. Three high tasks
        with 3 slots dispatch in id order."""
        _make_task(queue_dir, "h-c", priority="high")
        _make_task(queue_dir, "h-a", priority="high")
        _make_task(queue_dir, "h-b", priority="high")
        settings = _make_settings(initial=3, max_c=3)
        order = _capture_dispatch_order(queue_dir=queue_dir, settings=settings)
        assert order == ["h-a", "h-b", "h-c"]

    def test_default_priority_treated_as_normal(self, queue_dir: Path) -> None:
        """A task with no explicit priority is normal — it dispatches
        between high and low even though the YAML lacks the field."""
        _make_task(queue_dir, "default-task")  # no priority
        _make_task(queue_dir, "low-task", priority="low")
        _make_task(queue_dir, "high-task", priority="high")
        settings = _make_settings(initial=3, max_c=3)
        order = _capture_dispatch_order(queue_dir=queue_dir, settings=settings)
        assert order == ["high-task", "default-task", "low-task"]

    def test_high_priority_with_unmet_dependency_does_not_dispatch(self, queue_dir: Path) -> None:
        """Priority must not override the ``depends_on`` gate. A high task
        blocked on an incomplete dependency stays pending while lower-
        priority tasks dispatch ahead of it. Without this guarantee
        operators could starve a queue by marking everything ``high``."""
        _make_task(queue_dir, "z-high", priority="high", depends_on=["missing-dep"])
        _make_task(queue_dir, "a-normal", priority="normal")
        settings = _make_settings(initial=1, max_c=1)
        order = _capture_dispatch_order(queue_dir=queue_dir, settings=settings)
        assert order == ["a-normal"]


# ---------------------------------------------------------------------------
# CLI: ``queue list --order-by-dispatch``
# ---------------------------------------------------------------------------


class TestOrderByDispatchCLI:
    @pytest.fixture
    def cli(self) -> CliRunner:
        return CliRunner()

    def test_three_band_json_output_with_dispatch_rank(
        self, queue_dir: Path, cli: CliRunner
    ) -> None:
        """Each entry has a ``dispatch_rank`` starting from 1 and a
        ``sort_key`` matching the priority_sort_key tuple."""
        from claude_task_runner.cli.queue_cmd import app

        _make_task(queue_dir, "n-aaa", priority="normal")
        _make_task(queue_dir, "h-zzz", priority="high")
        _make_task(queue_dir, "l-mmm", priority="low")
        _make_task(queue_dir, "h-aaa", priority="high")
        result = cli.invoke(
            app,
            ["list", "--queue", str(queue_dir), "--json", "--order-by-dispatch"],
        )
        assert result.exit_code == 0, result.stdout
        payload = json.loads(result.stdout)
        ids = [t["id"] for t in payload["tasks"]]
        assert ids == ["h-aaa", "h-zzz", "n-aaa", "l-mmm"]
        ranks = [t["dispatch_rank"] for t in payload["tasks"]]
        assert ranks == [1, 2, 3, 4]
        # sort_key reflects the priority rank tuple.
        sort_keys = [tuple(t["sort_key"]) for t in payload["tasks"]]
        assert sort_keys == [(0, "h-aaa"), (0, "h-zzz"), (1, "n-aaa"), (2, "l-mmm")]

    def test_empty_queue_returns_empty_list(self, queue_dir: Path, cli: CliRunner) -> None:
        from claude_task_runner.cli.queue_cmd import app

        result = cli.invoke(
            app,
            ["list", "--queue", str(queue_dir), "--json", "--order-by-dispatch"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload == {"tasks": []}

    def test_unparseable_yaml_surfaces_as_error_entry(
        self, queue_dir: Path, cli: CliRunner
    ) -> None:
        """A malformed YAML is reported in its own entry with an
        ``error`` key — the operator sees it without losing visibility
        of the parseable tasks' dispatch order."""
        from claude_task_runner.cli.queue_cmd import app

        _make_task(queue_dir, "good-high", priority="high")
        bad = todo_dir(queue_dir) / "bad.yaml"
        bad.write_text("not yaml: : :", encoding="utf-8")

        result = cli.invoke(
            app,
            ["list", "--queue", str(queue_dir), "--json", "--order-by-dispatch"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        error_entries = [t for t in payload["tasks"] if "error" in t]
        ok_entries = [t for t in payload["tasks"] if "error" not in t]
        assert len(error_entries) == 1
        assert error_entries[0]["id"] == "bad"
        assert [t["id"] for t in ok_entries] == ["good-high"]
        assert ok_entries[0]["dispatch_rank"] == 1

    def test_human_readable_output_includes_rank_prefix(
        self, queue_dir: Path, cli: CliRunner
    ) -> None:
        """Without ``--json``, each task line is prefixed with ``#1``,
        ``#2``, etc. so the operator can read the dispatch order at a
        glance."""
        from claude_task_runner.cli.queue_cmd import app

        _make_task(queue_dir, "001-normal", priority="normal")
        _make_task(queue_dir, "999-high", priority="high")
        result = cli.invoke(
            app,
            ["list", "--queue", str(queue_dir), "--order-by-dispatch"],
        )
        assert result.exit_code == 0
        # The high-priority task is shown FIRST and prefixed with #1.
        out = result.stdout
        high_idx = out.find("999-high")
        normal_idx = out.find("001-normal")
        assert high_idx != -1 and normal_idx != -1
        assert high_idx < normal_idx
        assert "#  1" in out and "#  2" in out

    def test_default_filename_order_has_no_dispatch_rank(
        self, queue_dir: Path, cli: CliRunner
    ) -> None:
        """Without ``--order-by-dispatch`` the JSON entries do NOT have
        a ``dispatch_rank`` field — operators (and skills) can use the
        flag's presence as the canonical signal that the list is
        priority-ordered, not filename-ordered."""
        from claude_task_runner.cli.queue_cmd import app

        _make_task(queue_dir, "001-normal", priority="normal")
        _make_task(queue_dir, "999-high", priority="high")
        result = cli.invoke(app, ["list", "--queue", str(queue_dir), "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        ids = [t["id"] for t in payload["tasks"]]
        # Filename order, NOT priority order.
        assert ids == ["001-normal", "999-high"]
        assert all("dispatch_rank" not in t for t in payload["tasks"])
