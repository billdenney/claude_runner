from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from claude_runner.config import Settings
from claude_runner.models import TaskStatus
from claude_runner.notify.emitter import EventEmitter
from claude_runner.runner.subprocess_backend import SubprocessBackend
from claude_runner.state.store import StateStore
from claude_runner.todo.schema import build_task


def _spec(tmp_path: Path, settings: Settings, **overrides: Any):
    raw = {"prompt": "go", "working_dir": str(tmp_path), **overrides}
    return build_task(raw=raw, source_path=tmp_path / "001.yaml", settings=settings)


class _FakeStream:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)

    async def read(self) -> bytes:
        return b""


class _FakeProc:
    def __init__(self, stdout_lines: list[bytes], rc: int = 0) -> None:
        self.stdout = _FakeStream(stdout_lines)
        self.stderr = _FakeStream([])
        self._rc = rc

    async def wait(self) -> int:
        return self._rc


@pytest.mark.asyncio
async def test_subprocess_backend_parses_streamed_json(
    tmp_project: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_store = StateStore(tmp_project / ".claude_runner")
    emitter = EventEmitter(
        events_path=state_store.events_path(), log_dir=state_store.root / "logs", stdout=False
    )
    lines = [
        (
            json.dumps({"type": "system", "subtype": "init", "session_id": "sid-123"}) + "\n"
        ).encode(),
        (
            json.dumps(
                {
                    "type": "result",
                    "usage": {
                        "input_tokens": 1200,
                        "output_tokens": 600,
                        "cache_read_input_tokens": 100,
                    },
                    "total_cost_usd": 0.05,
                }
            )
            + "\n"
        ).encode(),
    ]

    async def fake_create(*_args, **_kwargs):
        return _FakeProc(lines, rc=0)

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    backend = SubprocessBackend(state_store=state_store, emitter=emitter)
    spec = _spec(tmp_project, settings)
    result = await backend.run_task(spec)

    assert result.success is True
    assert result.usage.input_tokens == 1200
    assert result.usage.cache_read_tokens == 100
    assert result.session_id == "sid-123"
    assert result.usage.cost_usd == 0.05
    assert state_store.load(spec.id).status is TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_subprocess_backend_reports_failure_on_nonzero_exit(
    tmp_project: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_store = StateStore(tmp_project / ".claude_runner")
    emitter = EventEmitter(
        events_path=state_store.events_path(), log_dir=state_store.root / "logs", stdout=False
    )

    async def fake_create(*_args, **_kwargs):
        return _FakeProc([b""], rc=2)

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    backend = SubprocessBackend(state_store=state_store, emitter=emitter)
    spec = _spec(tmp_project, settings)
    result = await backend.run_task(spec)
    assert result.success is False
    assert state_store.load(spec.id).status is TaskStatus.FAILED


@pytest.mark.asyncio
async def test_subprocess_backend_raises_without_binary(
    tmp_project: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_store = StateStore(tmp_project / ".claude_runner")
    emitter = EventEmitter(
        events_path=state_store.events_path(), log_dir=state_store.root / "logs", stdout=False
    )
    monkeypatch.setattr("shutil.which", lambda _: None)
    backend = SubprocessBackend(state_store=state_store, emitter=emitter)
    spec = _spec(tmp_project, settings)
    with pytest.raises(RuntimeError):
        await backend.run_task(spec)


@pytest.mark.asyncio
async def test_subprocess_backend_passes_required_cli_flags(
    tmp_project: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: ``--print`` + ``--output-format=stream-json`` needs ``--verbose``.

    The claude CLI (v2.x) exits immediately with

        Error: When using --print, --output-format=stream-json requires --verbose

    if ``--verbose`` is missing.  Capture the argv passed to
    ``asyncio.create_subprocess_exec`` and assert the required flag set is
    present, so a future refactor cannot silently drop ``--verbose`` again.
    """
    state_store = StateStore(tmp_project / ".claude_runner")
    emitter = EventEmitter(
        events_path=state_store.events_path(), log_dir=state_store.root / "logs", stdout=False
    )

    captured_args: list[tuple[str, ...]] = []

    async def fake_create(*args: str, **_kwargs: Any):
        captured_args.append(args)
        return _FakeProc([b""], rc=0)

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    backend = SubprocessBackend(state_store=state_store, emitter=emitter)
    spec = _spec(tmp_project, settings)
    await backend.run_task(spec)

    assert captured_args, "create_subprocess_exec was not called"
    args = captured_args[0]

    # --print/-p and --output-format stream-json are already required; --verbose
    # is the regression this test guards.
    assert "-p" in args
    assert "--output-format" in args
    assert args[args.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in args, (
        "subprocess backend must pass --verbose; otherwise claude CLI exits "
        "with 'When using --print, --output-format=stream-json requires --verbose'"
    )
