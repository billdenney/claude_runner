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
from claude_task_runner.runner.in_flight import DispatchSlot


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    todo_dir(qd)
    return qd


def _make_settings(*, initial: int = 1, max_c: int = 2) -> Any:
    """Minimal Settings-shaped object for the tick_consume path.

    tick_consume only touches ``concurrency.max_concurrency``,
    ``claude.executable``, and ``accounts`` (via resolve_accounts);
    the orchestrator's ``_dispatch_one_safely`` is patched in every
    test so the rest of the settings tree is irrelevant. Using
    SimpleNamespace keeps the test free of the full pydantic Settings
    dependency.
    """
    from claude_task_runner.config.schema import AccountSettings

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
        dispatch=SimpleNamespace(auto_detect_paths_in_prompt=False),
        accounts=[AccountSettings(name="default", config_dir="")],
    )


def _slot(task_id: str, thread: threading.Thread, account: str = "default") -> Any:
    """Build a DispatchSlot for tests that need to seed a pre-existing in-flight entry."""
    from claude_task_runner.runner.in_flight import DispatchSlot

    return DispatchSlot(
        task_id=task_id,
        account=account,
        started_at=datetime(2026, 5, 21, tzinfo=UTC),
        thread=thread,
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
        in_flight: dict[str, DispatchSlot] = {}
        n = fd_mod.tick_consume(
            queue_dir=queue_dir,
            settings=settings,
            clock=RealClock(),
            in_flight_slots=in_flight,
        )
        assert n == 0
        assert in_flight == {}

    def test_dispatches_pending_task(self, queue_dir: Path) -> None:
        _make_task(queue_dir, "t1")
        fd_mod.write_request(queue_dir, "t1")
        settings = _make_settings(max_c=2)
        in_flight: dict[str, DispatchSlot] = {}

        with patch(
            "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
            return_value=None,
        ):
            n = fd_mod.tick_consume(
                queue_dir=queue_dir,
                settings=settings,
                clock=RealClock(),
                in_flight_slots=in_flight,
            )
            assert n == 1
            assert "t1" in in_flight
            for slot in list(in_flight.values()):
                slot.thread.join(timeout=2)

        # Request file has been consumed.
        assert not fd_mod.request_path(queue_dir, "t1").exists()

    def test_drops_request_when_task_missing(self, queue_dir: Path) -> None:
        fd_mod.write_request(queue_dir, "ghost")
        settings = _make_settings()
        in_flight: dict[str, DispatchSlot] = {}
        with patch(
            "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
            return_value=None,
        ) as mock_dispatch:
            n = fd_mod.tick_consume(
                queue_dir=queue_dir,
                settings=settings,
                clock=RealClock(),
                in_flight_slots=in_flight,
            )
        assert n == 0
        assert not fd_mod.request_path(queue_dir, "ghost").exists()
        mock_dispatch.assert_not_called()

    def test_drops_request_when_status_not_dispatchable(self, queue_dir: Path) -> None:
        _make_task(queue_dir, "t1")
        _seed_state(queue_dir, "t1", "awaiting_sidecar")
        fd_mod.write_request(queue_dir, "t1")
        settings = _make_settings()
        in_flight: dict[str, DispatchSlot] = {}
        with patch(
            "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
            return_value=None,
        ) as mock_dispatch:
            n = fd_mod.tick_consume(
                queue_dir=queue_dir,
                settings=settings,
                clock=RealClock(),
                in_flight_slots=in_flight,
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
        in_flight = {"t1": _slot("t1", ghost)}

        settings = _make_settings()
        with patch(
            "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
            return_value=None,
        ) as mock_dispatch:
            n = fd_mod.tick_consume(
                queue_dir=queue_dir,
                settings=settings,
                clock=RealClock(),
                in_flight_slots=in_flight,
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
        in_flight = {"already-busy": _slot("already-busy", busy)}

        with patch(
            "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
            return_value=None,
        ) as mock_dispatch:
            n = fd_mod.tick_consume(
                queue_dir=queue_dir,
                settings=settings,
                clock=RealClock(),
                in_flight_slots=in_flight,
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
        in_flight = {"already-busy": _slot("already-busy", busy)}

        with patch(
            "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
            return_value=None,
        ):
            n = fd_mod.tick_consume(
                queue_dir=queue_dir,
                settings=settings,
                clock=RealClock(),
                in_flight_slots=in_flight,
            )
            assert n == 1
            assert "t1" in in_flight
            for tid in ["t1"]:
                in_flight[tid].thread.join(timeout=2)

        stop.set()
        busy.join(timeout=2)
        assert not fd_mod.request_path(queue_dir, "t1").exists()


# ---------------------------------------------------------------------------
# Corrupt-state handling (audit finding: missing-vs-corrupt distinction)
# ---------------------------------------------------------------------------


class TestLoadStateOrNone:
    """``_load_state_or_none`` must distinguish a *missing* state file
    (legitimately "no prior state" → ``None``) from a *corrupt* one
    (refuse — surfacing a ``ForceDispatchError`` rather than silently
    treating it as "no prior state", which would re-dispatch a task that
    may already be running or completed)."""

    def test_missing_state_returns_none(self, queue_dir: Path) -> None:
        assert fd_mod._load_state_or_none(queue_dir, "never-dispatched") is None

    def test_valid_state_round_trips(self, queue_dir: Path) -> None:
        _seed_state(queue_dir, "t1", "failed")
        state = fd_mod._load_state_or_none(queue_dir, "t1")
        assert state is not None
        assert state.status == "failed"

    def test_corrupt_state_raises(self, queue_dir: Path) -> None:
        state_path_for(queue_dir, "t1").write_text("not yaml: ][", encoding="utf-8")
        with pytest.raises(fd_mod.ForceDispatchError, match="unreadable/corrupt"):
            fd_mod._load_state_or_none(queue_dir, "t1")


class TestTickConsumeCorruptState:
    def test_corrupt_state_leaves_request_and_does_not_dispatch(self, queue_dir: Path) -> None:
        """A corrupt state YAML must NOT be treated as dispatchable. The
        request file is left in place (for operator repair) and no
        dispatch thread is spawned."""
        _make_task(queue_dir, "t1")
        state_path_for(queue_dir, "t1").write_text("not yaml: ][", encoding="utf-8")
        fd_mod.write_request(queue_dir, "t1")
        settings = _make_settings()
        in_flight: dict[str, DispatchSlot] = {}

        with patch(
            "claude_task_runner.runner.orchestrator.dispatcher_mod.dispatch",
            return_value=None,
        ) as mock_dispatch:
            n = fd_mod.tick_consume(
                queue_dir=queue_dir,
                settings=settings,
                clock=RealClock(),
                in_flight_slots=in_flight,
            )
        assert n == 0
        mock_dispatch.assert_not_called()
        assert in_flight == {}
        # Request PERSISTS so the operator can repair the state YAML.
        assert fd_mod.request_path(queue_dir, "t1").exists()


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

    def test_raises_when_state_corrupt(self, queue_dir: Path) -> None:
        """A corrupt state YAML must refuse rather than silently dispatch
        with a fresh state (which could re-run a completed task)."""
        _make_task(queue_dir, "t1")
        state_path_for(queue_dir, "t1").write_text("not yaml: ][", encoding="utf-8")
        settings = _make_settings()
        with pytest.raises(fd_mod.ForceDispatchError, match="unreadable/corrupt"):
            fd_mod.dispatch_synchronously(
                task_id="t1",
                queue_dir=queue_dir,
                settings=settings,
                clock=RealClock(),
            )


# ---------------------------------------------------------------------------
# ADR-0024 — session affinity in force-dispatch
# ---------------------------------------------------------------------------


def _make_multi_account_settings(*, max_c: int = 2) -> Any:
    """Two-account settings shape for affinity tests."""
    from claude_task_runner.config.schema import AccountSettings

    return SimpleNamespace(
        concurrency=SimpleNamespace(initial_concurrency=1, max_concurrency=max_c),
        task_caps=SimpleNamespace(),
        session=SimpleNamespace(),
        hooks=SimpleNamespace(),
        failure_classifier=None,
        claude=SimpleNamespace(executable="claude", config_dir=""),
        dispatch=SimpleNamespace(auto_detect_paths_in_prompt=False),
        accounts=[
            AccountSettings(name="personal", config_dir="/tmp/.claude_personal"),
            AccountSettings(name="work", config_dir="/tmp/.claude_work"),
        ],
    )


def _seed_sessioned_state(
    queue_dir: Path,
    task_id: str,
    *,
    session_id: str,
    session_account: str | None,
    status: str = "failed",
) -> TaskState:
    """State YAML with an active session affined to a specific account."""
    state = TaskState(
        task_id=task_id,
        status=status,
        session_id=session_id,
        session_account=session_account,
    )
    write_state_atomic(state, state_path_for(queue_dir, task_id))
    return state


class TestForceDispatchAffinity:
    """ADR-0024: force-dispatch (including ``--over-limit``) must NOT
    cross-account-resume. The throttle bypass is a policy choice; the
    affinity check is a correctness invariant — resuming under a
    different ``CLAUDE_CONFIG_DIR`` produces ``No conversation found
    with session ID``.
    """

    def test_tick_consume_routes_to_affined_account(self, queue_dir: Path) -> None:
        _make_task(queue_dir, "t1", account="personal")
        _seed_sessioned_state(queue_dir, "t1", session_id="sess1", session_account="work")
        fd_mod.write_request(queue_dir, "t1", allow_over_limit=True)
        settings = _make_multi_account_settings()
        in_flight: dict[str, DispatchSlot] = {}

        captured: dict[str, str | None] = {}

        def fake_spawn(**kwargs: object) -> None:
            captured["account"] = str(kwargs["account"])
            captured["config_dir"] = str(kwargs["claude_config_dir"])

        with patch.object(fd_mod, "_spawn_dispatch_thread", side_effect=fake_spawn):
            n = fd_mod.tick_consume(
                queue_dir=queue_dir,
                settings=settings,
                clock=RealClock(),
                in_flight_slots=in_flight,
            )
        assert n == 1
        # Affinity wins over both task.account=personal AND first-configured.
        assert captured["account"] == "work"
        assert captured["config_dir"] == "/tmp/.claude_work"

    def test_tick_consume_drops_request_for_orphaned_affined_account(self, queue_dir: Path) -> None:
        _make_task(queue_dir, "t1")
        _seed_sessioned_state(queue_dir, "t1", session_id="sess1", session_account="ghost")
        fd_mod.write_request(queue_dir, "t1", allow_over_limit=True)
        settings = _make_multi_account_settings()
        in_flight: dict[str, DispatchSlot] = {}

        with patch.object(fd_mod, "_spawn_dispatch_thread") as spawn:
            n = fd_mod.tick_consume(
                queue_dir=queue_dir,
                settings=settings,
                clock=RealClock(),
                in_flight_slots=in_flight,
            )
        assert n == 0
        spawn.assert_not_called()
        assert not fd_mod.request_path(queue_dir, "t1").exists()

    def test_dispatch_synchronously_raises_when_affined_account_missing(
        self, queue_dir: Path
    ) -> None:
        _make_task(queue_dir, "t1")
        _seed_sessioned_state(
            queue_dir,
            "t1",
            session_id="sess1",
            session_account="ghost",
            status="failed",
        )
        settings = _make_multi_account_settings()
        with pytest.raises(fd_mod.ForceDispatchError, match="restart-fresh"):
            fd_mod.dispatch_synchronously(
                task_id="t1",
                queue_dir=queue_dir,
                settings=settings,
                clock=RealClock(),
            )

    def test_tick_consume_falls_back_to_pinning_with_no_session(self, queue_dir: Path) -> None:
        """No session_id → no affinity constraint → honour task.account pin."""
        _make_task(queue_dir, "t1", account="work")
        # Seed state with no session at all.
        write_state_atomic(
            TaskState(task_id="t1", status="failed"),
            state_path_for(queue_dir, "t1"),
        )
        fd_mod.write_request(queue_dir, "t1", allow_over_limit=True)
        settings = _make_multi_account_settings()
        in_flight: dict[str, DispatchSlot] = {}

        captured: dict[str, str | None] = {}

        def fake_spawn(**kwargs: object) -> None:
            captured["account"] = str(kwargs["account"])

        with patch.object(fd_mod, "_spawn_dispatch_thread", side_effect=fake_spawn):
            n = fd_mod.tick_consume(
                queue_dir=queue_dir,
                settings=settings,
                clock=RealClock(),
                in_flight_slots=in_flight,
            )
        assert n == 1
        assert captured["account"] == "work"


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

    def test_missing_task_human_output(self, queue_dir: Path, runner_cli: CliRunner) -> None:
        """Without --json, missing task prints a red error to stdout."""
        from claude_task_runner.cli.queue_cmd import app

        result = runner_cli.invoke(app, ["force-dispatch", "ghost", "--queue", str(queue_dir)])
        assert result.exit_code == 2
        assert "not in todo" in result.stdout

    def test_corrupt_task_yaml_human_output(self, queue_dir: Path, runner_cli: CliRunner) -> None:
        """Corrupt task YAML produces a human error and non-zero exit."""
        from claude_task_runner.cli.queue_cmd import app

        task_path_for(queue_dir, "broken").write_text("not yaml: ][", encoding="utf-8")
        result = runner_cli.invoke(app, ["force-dispatch", "broken", "--queue", str(queue_dir)])
        assert result.exit_code == 2
        assert "task YAML invalid" in result.stdout

    def test_corrupt_task_yaml_json_output(self, queue_dir: Path, runner_cli: CliRunner) -> None:
        """Corrupt task YAML with --json emits structured error payload."""
        from claude_task_runner.cli.queue_cmd import app

        task_path_for(queue_dir, "broken").write_text("not yaml: ][", encoding="utf-8")
        result = runner_cli.invoke(
            app, ["force-dispatch", "broken", "--queue", str(queue_dir), "--json"]
        )
        assert result.exit_code == 2
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert "task YAML invalid" in payload["error"]

    def test_completed_state_human_output(self, queue_dir: Path, runner_cli: CliRunner) -> None:
        """Already-completed task rejects without --json too."""
        from claude_task_runner.cli.queue_cmd import app

        _make_task(queue_dir, "t1")
        _seed_state(queue_dir, "t1", "completed")
        result = runner_cli.invoke(app, ["force-dispatch", "t1", "--queue", str(queue_dir)])
        assert result.exit_code == 2
        assert "not dispatchable" in result.stdout

    def test_corrupt_state_treated_as_dispatchable(
        self, queue_dir: Path, runner_cli: CliRunner
    ) -> None:
        """A corrupt state YAML falls through to current_status=None, which
        the policy treats as dispatchable. Covers the
        (QueueIOError, QueueSchemaError) branch at queue_cmd.py:787-788.
        """
        from claude_task_runner.cli.queue_cmd import app

        _make_task(queue_dir, "t1")
        state_path_for(queue_dir, "t1").write_text("not yaml: ][", encoding="utf-8")
        fake_state = TaskState(task_id="t1", status="completed", attempts=1, stop_reason="end_turn")
        with patch(
            "claude_task_runner.cli.queue_cmd.fd_mod.dispatch_synchronously",
            return_value=fake_state,
        ):
            result = runner_cli.invoke(
                app, ["force-dispatch", "t1", "--queue", str(queue_dir), "--json"]
            )
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["status"] == "completed"

    def test_synchronous_error_human_output(self, queue_dir: Path, runner_cli: CliRunner) -> None:
        """ForceDispatchError surfaces as a red error line in non-JSON mode."""
        from claude_task_runner.cli.queue_cmd import app

        _make_task(queue_dir, "t1")
        with patch(
            "claude_task_runner.cli.queue_cmd.fd_mod.dispatch_synchronously",
            side_effect=fd_mod.ForceDispatchError("hook failed"),
        ):
            result = runner_cli.invoke(app, ["force-dispatch", "t1", "--queue", str(queue_dir)])
        assert result.exit_code == 2
        assert "force-dispatch failed" in result.stdout

    def test_supervised_picked_up_human_output(
        self, queue_dir: Path, runner_cli: CliRunner
    ) -> None:
        """Supervised path with picked_up=True prints the running confirmation."""
        from claude_task_runner.cli.queue_cmd import app

        _make_task(queue_dir, "t1")
        with (
            patch("claude_task_runner.cli.queue_cmd._supervisor_is_alive", return_value=True),
            patch("claude_task_runner.cli.queue_cmd._poll_until_running", return_value=True),
        ):
            result = runner_cli.invoke(
                app,
                [
                    "force-dispatch",
                    "t1",
                    "--queue",
                    str(queue_dir),
                    "--wait-seconds",
                    "1",
                ],
            )
        assert result.exit_code == 0
        assert "entered `running` status" in result.stdout

    def test_supervised_timeout_human_output(self, queue_dir: Path, runner_cli: CliRunner) -> None:
        """Supervised path with wait>0 but picked_up=False prints the yellow timeout note."""
        from claude_task_runner.cli.queue_cmd import app

        _make_task(queue_dir, "t1")
        with (
            patch("claude_task_runner.cli.queue_cmd._supervisor_is_alive", return_value=True),
            patch("claude_task_runner.cli.queue_cmd._poll_until_running", return_value=False),
        ):
            result = runner_cli.invoke(
                app,
                [
                    "force-dispatch",
                    "t1",
                    "--queue",
                    str(queue_dir),
                    "--wait-seconds",
                    "1",
                ],
            )
        assert result.exit_code == 0
        assert "still not running" in result.stdout

    def test_poll_until_running_observes_terminal_status(self, queue_dir: Path) -> None:
        """A completed/failed/awaiting_sidecar state also returns True — the
        task raced through 'running'. Covers queue_cmd.py:878.
        """
        from claude_task_runner.cli.queue_cmd import _poll_until_running

        _make_task(queue_dir, "t1")
        _seed_state(queue_dir, "t1", "completed")
        assert _poll_until_running(queue_dir, "t1", wait_seconds=1.0) is True

    def test_poll_until_running_handles_corrupt_state(self, queue_dir: Path) -> None:
        """Corrupt state YAML is treated as status=None and the loop continues.
        Covers queue_cmd.py:872-873.
        """
        from claude_task_runner.cli.queue_cmd import _poll_until_running

        _make_task(queue_dir, "t1")
        state_path_for(queue_dir, "t1").write_text("not yaml: ][", encoding="utf-8")
        # Loop returns False (never sees a real running status) within the budget.
        assert _poll_until_running(queue_dir, "t1", wait_seconds=0.6) is False


class TestForceDispatchReadinessGate:
    """Force overrides the THROTTLE, not a missing input (ADR-0030).

    A `requires` element says the file this run reads is not on disk. Forcing
    past one buys a worker that can only discover the gap, file a sidecar and
    exit — the dispatch/re-file loop the gate exists to prevent. So both
    force paths enforce it, and both say which element is missing.
    """

    def test_tick_consume_refuses_task_with_unmet_requirement(self, queue_dir: Path) -> None:
        _make_task(queue_dir, "t1", requires=[{"kind": "file", "path": "inputs/missing.md"}])
        _seed_state(queue_dir, "t1", "pending")
        fd_mod.write_request(queue_dir, "t1", allow_over_limit=True)

        with patch.object(fd_mod, "_spawn_dispatch_thread") as spawn:
            n = fd_mod.tick_consume(
                queue_dir=queue_dir,
                settings=_make_settings(),
                clock=RealClock(),
                in_flight_slots={},
            )

        assert n == 0
        spawn.assert_not_called()

    def test_tick_consume_consumes_the_refused_request(self, queue_dir: Path) -> None:
        """Dropped, not left to fire later: a "force NOW" that silently
        re-arms itself would surprise the operator, and the ordinary selector
        admits the task the tick after the file appears anyway."""
        _make_task(queue_dir, "t1", requires=[{"kind": "file", "path": "inputs/missing.md"}])
        _seed_state(queue_dir, "t1", "pending")
        fd_mod.write_request(queue_dir, "t1", allow_over_limit=True)

        with patch.object(fd_mod, "_spawn_dispatch_thread"):
            fd_mod.tick_consume(
                queue_dir=queue_dir,
                settings=_make_settings(),
                clock=RealClock(),
                in_flight_slots={},
            )

        assert fd_mod.list_requests(queue_dir) == []

    def test_tick_consume_dispatches_once_requirement_is_met(self, queue_dir: Path) -> None:
        _make_task(queue_dir, "t1", requires=[{"kind": "file", "path": "inputs/present.md"}])
        _seed_state(queue_dir, "t1", "pending")
        target = queue_dir / "inputs" / "present.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("trimmed", encoding="utf-8")
        fd_mod.write_request(queue_dir, "t1", allow_over_limit=True)

        with patch.object(fd_mod, "_spawn_dispatch_thread") as spawn:
            n = fd_mod.tick_consume(
                queue_dir=queue_dir,
                settings=_make_settings(),
                clock=RealClock(),
                in_flight_slots={},
            )

        assert n == 1
        spawn.assert_called_once()

    def test_dispatch_synchronously_raises_naming_the_missing_element(
        self, queue_dir: Path
    ) -> None:
        _make_task(queue_dir, "t1", requires=[{"kind": "file", "path": "inputs/missing.md"}])
        _seed_state(queue_dir, "t1", "pending")

        with pytest.raises(fd_mod.ForceDispatchError, match=r"missing\.md"):
            fd_mod.dispatch_synchronously(
                task_id="t1",
                queue_dir=queue_dir,
                settings=_make_settings(),
                clock=RealClock(),
            )
