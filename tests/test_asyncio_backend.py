from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from claude_runner.config import Settings
from claude_runner.models import TaskStatus
from claude_runner.notify.emitter import EventEmitter
from claude_runner.state.store import StateStore
from claude_runner.todo.schema import build_task


def _install_fake_sdk(monkeypatch: pytest.MonkeyPatch, *, queries: list[dict]) -> None:
    """Inject a fake claude_agent_sdk module with controllable behavior."""
    fake_mod = types.ModuleType("claude_agent_sdk")

    class ClaudeAgentOptions:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            fake_mod.captured_options = self  # type: ignore[attr-defined]

    class HookMatcher:
        def __init__(self, *, hooks: list[Any], matcher: str | None = None) -> None:
            self.hooks = hooks
            self.matcher = matcher

    class _Message:
        def __init__(self, **attrs: Any) -> None:
            for k, v in attrs.items():
                setattr(self, k, v)

    async def query(*, prompt: str, options: Any):
        for msg in queries:
            yield _Message(**msg)

    fake_mod.ClaudeAgentOptions = ClaudeAgentOptions  # type: ignore[attr-defined]
    fake_mod.HookMatcher = HookMatcher  # type: ignore[attr-defined]
    fake_mod.query = query  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_mod)


def _spec(tmp_path: Path, settings: Settings):
    raw = {"prompt": "go", "working_dir": str(tmp_path)}
    return build_task(raw=raw, source_path=tmp_path / "001.yaml", settings=settings)


@pytest.mark.asyncio
async def test_asyncio_backend_captures_session_and_usage(
    tmp_project: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_sdk(
        monkeypatch,
        queries=[
            {"subtype": "init", "data": {"session_id": "sid-XYZ"}},
            {
                "subtype": "success",
                "usage": {
                    "input_tokens": 1500,
                    "output_tokens": 400,
                    "cache_read_input_tokens": 50,
                },
                "total_cost_usd": 0.02,
            },
        ],
    )
    from claude_runner.runner.asyncio_backend import AsyncioBackend

    state_store = StateStore(tmp_project / ".claude_runner")
    emitter = EventEmitter(
        events_path=state_store.events_path(), log_dir=state_store.root / "logs", stdout=False
    )
    backend = AsyncioBackend(state_store=state_store, emitter=emitter)
    spec = _spec(tmp_project, settings)

    # Before the run starts, manually register the Stop hook so success path
    # gets marked COMPLETED (in real SDK the Stop hook would fire).
    result = await backend.run_task(spec)
    # The fake SDK never fires Stop, so status remains RUNNING on the store —
    # the backend reconciles to RUNNING; assert that usage and session were captured.
    final = state_store.load(spec.id)
    assert final.session_id == "sid-XYZ"
    assert result.usage.input_tokens == 1500
    assert result.usage.output_tokens == 400
    assert result.usage.cache_read_tokens == 50


@pytest.mark.asyncio
async def test_asyncio_backend_handles_exception(
    tmp_project: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_mod = types.ModuleType("claude_agent_sdk")

    class ClaudeAgentOptions:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    class HookMatcher:
        def __init__(self, *, hooks: list[Any]) -> None:
            self.hooks = hooks

    async def query(*, prompt: str, options: Any):
        raise RuntimeError("connection reset")
        yield  # pragma: no cover

    fake_mod.ClaudeAgentOptions = ClaudeAgentOptions  # type: ignore[attr-defined]
    fake_mod.HookMatcher = HookMatcher  # type: ignore[attr-defined]
    fake_mod.query = query  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_mod)

    from claude_runner.runner.asyncio_backend import AsyncioBackend

    state_store = StateStore(tmp_project / ".claude_runner")
    emitter = EventEmitter(
        events_path=state_store.events_path(), log_dir=state_store.root / "logs", stdout=False
    )
    backend = AsyncioBackend(state_store=state_store, emitter=emitter)
    spec = _spec(tmp_project, settings)
    result = await backend.run_task(spec)
    assert result.success is False
    assert result.error is not None and "connection reset" in result.error
    assert state_store.load(spec.id).status is TaskStatus.FAILED


@pytest.mark.asyncio
async def test_asyncio_backend_errors_without_sdk(
    tmp_project: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)  # type: ignore[arg-type]
    from claude_runner.runner.asyncio_backend import AsyncioBackend

    state_store = StateStore(tmp_project / ".claude_runner")
    emitter = EventEmitter(
        events_path=state_store.events_path(), log_dir=state_store.root / "logs", stdout=False
    )
    backend = AsyncioBackend(state_store=state_store, emitter=emitter)
    spec = _spec(tmp_project, settings)
    with pytest.raises(RuntimeError, match="claude-agent-sdk"):
        await backend.run_task(spec)
