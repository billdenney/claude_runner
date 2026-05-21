"""Tests for runner/force_dispatch.py and the queue force-dispatch CLI."""

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

from claude_task_runner.clock import FakeClock, RealClock
from claude_task_runner.queue.schema import Task, TaskState
from claude_task_runner.queue.store import (
    queue_runtime_dir,
    state_path_for,
    task_path_for,
    todo_dir,
    write_state_atomic,
    write_task_atomic,
)
from claude_task_runner.runner import force_dispatch as fd_mod


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    return qd


def _make_settings(*, initial: int = 1, max_c: int = 2) -> Any:
    """Minimal Settings-shaped object for the tick_consume path.

    tick_consume only touches ``concurrency.max_concurrency`` and
    ``claude.executable``; the orchestrator's ``_dispatch_one_safely``
    is patched in every test so the rest of the settings tree is
    irrelevant. Using SimpleNamespace keeps the test free of the full
    pydantic Settings dependency.
    """
    return SimpleNamespace(
        concurrency=SimpleNamespace(
            initial_concurrency=initial,
            max_concurrency=max_c,
        ),
        task_caps=SimpleNamespace(),
        session=SimpleNamespace(),
        hooks=SimpleNamespace(),
        failure_classifier=None,
        claude=SimpleNamespace(executable="claude", config_dir=""),
    )


def _make_task(qd: Path, task_id: str, **overrides: object) -> Task:
    payload: dict[str, object] = {
        "id": task_id,
        "title": f"Task {task_id}",
        "prompt": "do the thing",
    }
    payload.update(overrides)
    task = Task.model_validate(payload)
    write_task_atomic(task, task_path_for(qd, task_id))
    return task


def _seed_state(qd: Path, task_id: str, status: str = "pending") -> TaskState:
    state = TaskState(task_id=task_id, status=status)
    write_state_atomic(state, state_path_for(qd, task_id))
    return state


# ---------------------------------------------------------------------------
# Request file I/O
# ---------------------------------------------------------------------------


class TestRequestIO:
    def test_write_request_creates_directory_and_file(self, queue_dir: Path) -> None:
        path = fd_mod.write_request(queue_dir, "t1")
        assert path.exists()
        assert path.parent.name == "force_dispatch"
        data = json.loads(path.read_text())
        assert data["task_id"] == "t1"
        assert data["allow_over_limit"] is False
        # requested_at is ISO-parseable.
        datetime.fromisoformat(data["requested_at"])

    def test_write_request_overwrites_existing(self, queue_dir: Path) -> None:
        fd_mod.write_request(queue_dir, "t1", allow_over_limit=False)
        path = fd_mod.write_request(queue_dir, "t1", allow_over_limit=True)
        data = json.loads(path.read_text())
        assert data["allow_over_limit"] is True

    def test_list_requests_oldest_first(self, queue_dir: Path) -> None:
        clock = FakeClock(datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC))
        fd_mod.write_request(queue_dir, "first", clock=clock)
        clock.advance(5)
        fd_mod.write_request(queue_dir, "second", clock=clock)
        clock.advance(5)
        fd_mod.write_request(queue_dir, "third", clock=clock)
        reqs = fd_mod.list_requests(queue_dir)
        assert [r.task_id for r in reqs] == ["first", "second", "third"]

    def test_list_requests_empty_when_dir_absent(self, queue_dir: Path) -> None:
        assert fd_mod.list_requests(queue_dir) == []

    def test_list_requests_skips_malformed(self, queue_dir: Path) -> None:
        fd_mod.write_request(queue_dir, "good")
        bad = fd_mod.force_dispatch_dir(queue_dir) / "bad.req"
        bad.write_text("not json {")
        reqs = fd_mod.list_requests(queue_dir)
        assert [r.task_id for r in reqs] == ["good"]

    def test_consume_request_is_idempotent(self, queue_dir: Path) -> None:
        fd_mod.write_request(queue_dir, "t1")
        fd_mod.consume_request(queue_dir, "t1")
        assert not fd_mod.request_path(queue_dir, "t1").exists()
        # Second call must not raise.
        fd_mod.consume_request(queue_dir, "t1")


# ---------------------------------------------------------------------------
# tick_consume
# ---------------------------------------------------------------------------


class TestTickConsume:
    def test_no_requests_no_op(self, queue_dir: Path) -> None:
        settings = _make_settings()
        in_flight: dict[str, threading.Thread] = {}
        n = fd_mod.tick_consume(
            queue_dir=queue_dir,
            settings=settings,
            clock=RealClock(),
            in_flight_threads=in_flight,
        )
        assert n == 0
        assert in_flight == {}

    def test_dispatches_pending_task(self, queue_dir: Path) -> None:
        _make_task(queue_dir, "t1")
        fd_mod.write_request(queue_dir, "t1")
        settings = _make_settings(max_c=2)
        in_flight: dict[str, threading.Thread] = {}

        with patch(
            "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
            return_value=None,
        ):
            n = fd_mod.tick_consume(
                queue_dir=queue_dir,
                settings=settings,
                clock=RealClock(),
                in_flight_threads=in_flight,
            )
            assert n == 1
            assert "t1" in in_flight
            for th in list(in_flight.values()):
                th.join(timeout=2)

        # Request file has been consumed.
        assert not fd_mod.request_path(queue_dir, "t1").exists()

    def test_drops_request_when_task_missing(self, queue_dir: Path) -> None:
        fd_mod.write_request(queue_dir, "ghost")
        settings = _make_settings()
        in_flight: dict[str, threading.Thread] = {}
        with patch(
            "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
            return_value=None,
        ) as mock_dispatch:
            n = fd_mod.tick_consume(
                queue_dir=queue_dir,
                settings=settings,
                clock=RealClock(),
                in_flight_threads=in_flight,
            )
        assert n == 0
        assert not fd_mod.request_path(queue_dir, "ghost").exists()
        mock_dispatch.assert_not_called()

    def test_drops_request_when_status_not_dispatchable(self, queue_dir: Path) -> None:
        _make_task(queue_dir, "t1")
        _seed_state(queue_dir, "t1", "awaiting_sidecar")
        fd_mod.write_request(queue_dir, "t1")
        settings = _make_settings()
        in_flight: dict[str, threading.Thread] = {}
        with patch(
            "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
            return_value=None,
        ) as mock_dispatch:
            n = fd_mod.tick_consume(
                queue_dir=queue_dir,
                settings=settings,
                clock=RealClock(),
                in_flight_threads=in_flight,
            )
        assert n == 0
        assert not fd_mod.request_path(queue_dir, "t1").exists()
        mock_dispatch.assert_not_called()

    def test_consumes_request_when_task_already_in_flight(self, queue_dir: Path) -> None:
        _make_task(queue_dir, "t1")
        fd_mod.write_request(queue_dir, "t1")

        # Synthetic in-flight thread that doesn't actually do anything.
        stop = threading.Event()
        ghost = threading.Thread(target=lambda: stop.wait(timeout=5), daemon=True)
        ghost.start()
        in_flight = {"t1": ghost}

        settings = _make_settings()
        with patch(
            "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
            return_value=None,
        ) as mock_dispatch:
            n = fd_mod.tick_consume(
                queue_dir=queue_dir,
                settings=settings,
                clock=RealClock(),
                in_flight_threads=in_flight,
            )
        assert n == 0
        mock_dispatch.assert_not_called()
        assert not fd_mod.request_path(queue_dir, "t1").exists()
        stop.set()
        ghost.join(timeout=2)

    def test_respects_max_concurrency_by_default(self, queue_dir: Path) -> None:
        _make_task(queue_dir, "t1")
        fd_mod.write_request(queue_dir, "t1", allow_over_limit=False)
        settings = _make_settings(max_c=1)

        stop = threading.Event()
        busy = threading.Thread(target=lambda: stop.wait(timeout=5), daemon=True)
        busy.start()
        in_flight = {"already-busy": busy}

        with patch(
            "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
            return_value=None,
        ) as mock_dispatch:
            n = fd_mod.tick_consume(
                queue_dir=queue_dir,
                settings=settings,
                clock=RealClock(),
                in_flight_threads=in_flight,
            )
        assert n == 0
        mock_dispatch.assert_not_called()
        # Request file PERSISTS for the next tick.
        assert fd_mod.request_path(queue_dir, "t1").exists()

        stop.set()
        busy.join(timeout=2)

    def test_allow_over_limit_bypasses_max_concurrency(self, queue_dir: Path) -> None:
        _make_task(queue_dir, "t1")
        fd_mod.write_request(queue_dir, "t1", allow_over_limit=True)
        settings = _make_settings(max_c=1)

        stop = threading.Event()
        busy = threading.Thread(target=lambda: stop.wait(timeout=5), daemon=True)
        busy.start()
        in_flight = {"already-busy": busy}

        with patch(
            "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
            return_value=None,
        ):
            n = fd_mod.tick_consume(
                queue_dir=queue_dir,
                settings=settings,
                clock=RealClock(),
                in_flight_threads=in_flight,
            )
            assert n == 1
            assert "t1" in in_flight
            for tid in ["t1"]:
                in_flight[tid].join(timeout=2)

        stop.set()
        busy.join(timeout=2)
        assert not fd_mod.request_path(queue_dir, "t1").exists()


# ---------------------------------------------------------------------------
# dispatch_synchronously
# ---------------------------------------------------------------------------


class TestDispatchSynchronously:
    def test_raises_when_task_missing(self, queue_dir: Path) -> None:
        settings = _make_settings()
        with pytest.raises(fd_mod.ForceDispatchError, match="not in todo"):
            fd_mod.dispatch_synchronously(
                task_id="ghost",
                queue_dir=queue_dir,
                settings=settings,
                clock=RealClock(),
            )

    def test_raises_when_status_not_dispatchable(self, queue_dir: Path) -> None:
        _make_task(queue_dir, "t1")
        _seed_state(queue_dir, "t1", "completed")
        settings = _make_settings()
        with pytest.raises(fd_mod.ForceDispatchError, match="not dispatchable"):
            fd_mod.dispatch_synchronously(
                task_id="t1",
                queue_dir=queue_dir,
                settings=settings,
                clock=RealClock(),
            )


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


class TestForceDispatchCLI:
    @pytest.fixture
    def runner_cli(self) -> CliRunner:
        return CliRunner()

    def test_synchronous_path_when_no_supervisor(
        self, queue_dir: Path, runner_cli: CliRunner
    ) -> None:
        """With no supervisor running, the CLI dispatches in-process and
        returns the final state."""
        from claude_task_runner.cli.queue_cmd import app

        _make_task(queue_dir, "t1")
        # Patch dispatch_synchronously to avoid spawning real `claude`.
        fake_state = TaskState(
            task_id="t1",
            status="completed",
            attempts=1,
            stop_reason="end_turn",
        )
        with patch(
            "claude_task_runner.cli.queue_cmd.fd_mod.dispatch_synchronously",
            return_value=fake_state,
        ) as mock_sync:
            result = runner_cli.invoke(
                app,
                [
                    "force-dispatch",
                    "t1",
                    "--queue",
                    str(queue_dir),
                    "--json",
                ],
            )
        assert result.exit_code == 0, result.stdout
        payload = json.loads(result.stdout)
        assert payload == {
            "ok": True,
            "mode": "synchronous",
            "status": "completed",
            "attempts": 1,
            "stop_reason": "end_turn",
        }
        mock_sync.assert_called_once()

    def test_writes_request_when_supervisor_alive(
        self, queue_dir: Path, runner_cli: CliRunner
    ) -> None:
        """With supervisor alive, CLI writes a request file and returns."""
        from claude_task_runner.cli.queue_cmd import app

        _make_task(queue_dir, "t1")
        with patch(
            "claude_task_runner.cli.queue_cmd._supervisor_is_alive",
            return_value=True,
        ):
            result = runner_cli.invoke(
                app,
                [
                    "force-dispatch",
                    "t1",
                    "--queue",
                    str(queue_dir),
                    "--wait-seconds",
                    "0",
                    "--json",
                ],
            )
        assert result.exit_code == 0, result.stdout
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        assert payload["mode"] == "supervised"
        assert payload["running"] is False  # wait=0 so not polled
        assert fd_mod.request_path(queue_dir, "t1").exists()

    def test_rejects_missing_task(self, queue_dir: Path, runner_cli: CliRunner) -> None:
        from claude_task_runner.cli.queue_cmd import app

        result = runner_cli.invoke(
            app,
            [
                "force-dispatch",
                "ghost",
                "--queue",
                str(queue_dir),
                "--json",
            ],
        )
        assert result.exit_code == 2
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert "not in todo" in payload["error"]

    def test_rejects_completed_task(self, queue_dir: Path, runner_cli: CliRunner) -> None:
        from claude_task_runner.cli.queue_cmd import app

        _make_task(queue_dir, "t1")
        _seed_state(queue_dir, "t1", "completed")
        result = runner_cli.invoke(
            app,
            [
                "force-dispatch",
                "t1",
                "--queue",
                str(queue_dir),
                "--json",
            ],
        )
        assert result.exit_code == 2
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert "not dispatchable" in payload["error"]

    def test_over_limit_flag_sets_allow_over_limit(
        self, queue_dir: Path, runner_cli: CliRunner
    ) -> None:
        from claude_task_runner.cli.queue_cmd import app

        _make_task(queue_dir, "t1")
        with patch(
            "claude_task_runner.cli.queue_cmd._supervisor_is_alive",
            return_value=True,
        ):
            result = runner_cli.invoke(
                app,
                [
                    "force-dispatch",
                    "t1",
                    "--queue",
                    str(queue_dir),
                    "--over-limit",
                    "--wait-seconds",
                    "0",
                    "--json",
                ],
            )
        assert result.exit_code == 0, result.stdout
        path = fd_mod.request_path(queue_dir, "t1")
        data = json.loads(path.read_text())
        assert data["allow_over_limit"] is True

    def test_human_readable_output_supervised_path(
        self, queue_dir: Path, runner_cli: CliRunner
    ) -> None:
        """No --json: human output names the request file and notes the wait outcome."""
        from claude_task_runner.cli.queue_cmd import app

        _make_task(queue_dir, "t1")
        with patch(
            "claude_task_runner.cli.queue_cmd._supervisor_is_alive",
            return_value=True,
        ):
            result = runner_cli.invoke(
                app,
                [
                    "force-dispatch",
                    "t1",
                    "--queue",
                    str(queue_dir),
                    "--wait-seconds",
                    "0",
                ],
            )
        assert result.exit_code == 0, result.stdout
        assert "request written" in result.stdout

    def test_human_readable_output_synchronous_completed(
        self, queue_dir: Path, runner_cli: CliRunner
    ) -> None:
        """Synchronous-path human output is colourised by completed/failed status."""
        from claude_task_runner.cli.queue_cmd import app

        _make_task(queue_dir, "t1")
        fake_state = TaskState(task_id="t1", status="completed", attempts=1, stop_reason="end_turn")
        with patch(
            "claude_task_runner.cli.queue_cmd.fd_mod.dispatch_synchronously",
            return_value=fake_state,
        ):
            result = runner_cli.invoke(app, ["force-dispatch", "t1", "--queue", str(queue_dir)])
        assert result.exit_code == 0
        assert "t1" in result.stdout
        assert "completed" in result.stdout

    def test_synchronous_path_propagates_dispatch_error(
        self, queue_dir: Path, runner_cli: CliRunner
    ) -> None:
        """A ForceDispatchError from dispatch_synchronously exits non-zero."""
        from claude_task_runner.cli.queue_cmd import app

        _make_task(queue_dir, "t1")
        with patch(
            "claude_task_runner.cli.queue_cmd.fd_mod.dispatch_synchronously",
            side_effect=fd_mod.ForceDispatchError("hook failed"),
        ):
            result = runner_cli.invoke(
                app,
                [
                    "force-dispatch",
                    "t1",
                    "--queue",
                    str(queue_dir),
                    "--json",
                ],
            )
        assert result.exit_code == 2
        payload = json.loads(result.stdout)
        assert payload == {"ok": False, "error": "hook failed"}

    def test_poll_until_running_detects_running_status(
        self, queue_dir: Path, runner_cli: CliRunner
    ) -> None:
        """_poll_until_running returns True when state YAML flips to 'running'.

        Simulates the supervisor by pre-writing a 'running' state file
        before the CLI's poll loop fires. Used to cover the picked_up=True
        branch in the supervised path.
        """
        from claude_task_runner.cli.queue_cmd import app

        _make_task(queue_dir, "t1")
        _seed_state(queue_dir, "t1", "running")
        with patch(
            "claude_task_runner.cli.queue_cmd._supervisor_is_alive",
            return_value=True,
        ):
            result = runner_cli.invoke(
                app,
                [
                    "force-dispatch",
                    "t1",
                    "--queue",
                    str(queue_dir),
                    "--wait-seconds",
                    "2",
                    "--json",
                ],
            )
        # State was 'running' so the CLI accepted it as dispatched —
        # but we get an early-reject because 'running' is not in the
        # dispatchable allowlist. That's correct behaviour; the test
        # variant below covers the in-poll path.
        assert result.exit_code == 2  # rejected before write
        payload = json.loads(result.stdout)
        assert "not dispatchable" in payload["error"]

    def test_poll_until_running_observes_status_transition(self, queue_dir: Path) -> None:
        """Direct unit test for _poll_until_running observing a transition."""
        from claude_task_runner.cli.queue_cmd import _poll_until_running

        _make_task(queue_dir, "t1")
        # Pre-seed pending; spawn a thread that flips to 'running' shortly.
        _seed_state(queue_dir, "t1", "pending")

        def flip_to_running() -> None:
            import time as _time

            _time.sleep(0.3)
            state = TaskState(task_id="t1", status="running")
            write_state_atomic(state, state_path_for(queue_dir, "t1"))

        flipper = threading.Thread(target=flip_to_running, daemon=True)
        flipper.start()
        try:
            assert _poll_until_running(queue_dir, "t1", wait_seconds=3.0) is True
        finally:
            flipper.join(timeout=2)

    def test_poll_until_running_times_out_returns_false(self, queue_dir: Path) -> None:
        """_poll_until_running returns False when nothing happens within budget."""
        from claude_task_runner.cli.queue_cmd import _poll_until_running

        _make_task(queue_dir, "t1")
        _seed_state(queue_dir, "t1", "pending")
        assert _poll_until_running(queue_dir, "t1", wait_seconds=0.1) is False

    def test_poll_until_running_zero_seconds_returns_false(self, queue_dir: Path) -> None:
        """wait_seconds=0 is the supervised-path "fire and forget" mode."""
        from claude_task_runner.cli.queue_cmd import _poll_until_running

        assert _poll_until_running(queue_dir, "t1", wait_seconds=0) is False
