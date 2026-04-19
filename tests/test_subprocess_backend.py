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


class _FakeStdin:
    """Captures whatever the backend pipes in so tests can assert on it."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.closed = False

    def write(self, chunk: bytes) -> None:
        self.buf.extend(chunk)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _FakeProc:
    def __init__(self, stdout_lines: list[bytes], rc: int = 0) -> None:
        self.stdin = _FakeStdin()
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


@pytest.mark.asyncio
async def test_subprocess_backend_passes_resume_flag(
    tmp_project: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prior session id on the state file turns into `--resume <sid>` argv."""
    state_store = StateStore(tmp_project / ".claude_runner")
    from claude_runner.models import TaskState

    state_store.save(TaskState(task_id="001", session_id="prior-sid"))

    emitter = EventEmitter(
        events_path=state_store.events_path(), log_dir=state_store.root / "logs", stdout=False
    )
    captured: dict[str, Any] = {}

    async def fake_create(*args, **_kwargs):
        captured["argv"] = list(args)
        return _FakeProc([], rc=0)

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    backend = SubprocessBackend(state_store=state_store, emitter=emitter)
    spec = _spec(tmp_project, settings)
    await backend.run_task(spec)

    argv = captured["argv"]
    assert "--resume" in argv
    assert "prior-sid" in argv


@pytest.mark.asyncio
async def test_subprocess_backend_pipes_prompt_via_stdin_not_argv(
    tmp_project: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prompt text must never appear in argv — putting it on the command
    line means any filename/keyword in the prompt is visible in
    ``/proc/<pid>/cmdline`` and can be matched by a sub-agent's
    ``pkill -f <keyword>``, SIGTERMing the agent itself.

    Regression guard: exact-string prompt must be written to stdin; it must
    NOT appear as any argv element.
    """
    state_store = StateStore(tmp_project / ".claude_runner")
    emitter = EventEmitter(
        events_path=state_store.events_path(),
        log_dir=state_store.root / "logs",
        stdout=False,
    )

    captured: dict[str, Any] = {}
    fake_proc: list[_FakeProc] = []

    async def fake_create(*args, **_kwargs):
        captured["argv"] = list(args)
        p = _FakeProc([], rc=0)
        fake_proc.append(p)
        return p

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    distinctive_prompt = "Zebra-Mango-Prompt-7F3A please kill -f this"
    backend = SubprocessBackend(state_store=state_store, emitter=emitter)
    spec = _spec(tmp_project, settings, prompt=distinctive_prompt)
    await backend.run_task(spec)

    argv = captured["argv"]
    # The prompt MUST NOT be in the argv.
    assert distinctive_prompt not in argv, (
        "Prompt found in argv — this makes the prompt visible in "
        "/proc/<pid>/cmdline and lets sub-agents self-SIGTERM via pkill -f."
    )
    for piece in distinctive_prompt.split():
        assert piece not in argv, f"Prompt fragment {piece!r} leaked into argv"

    # The prompt MUST be on stdin.
    assert fake_proc, "subprocess was never started"
    assert fake_proc[0].stdin.buf.decode("utf-8") == distinctive_prompt
    assert fake_proc[0].stdin.closed is True


@pytest.mark.asyncio
async def test_subprocess_backend_reports_failure_when_subprocess_raises(
    tmp_project: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exception from create_subprocess_exec is caught and surfaces as FAILED."""
    state_store = StateStore(tmp_project / ".claude_runner")
    emitter = EventEmitter(
        events_path=state_store.events_path(), log_dir=state_store.root / "logs", stdout=False
    )

    async def fake_create(*_args, **_kwargs):
        raise OSError("cannot exec claude")

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    backend = SubprocessBackend(state_store=state_store, emitter=emitter)
    spec = _spec(tmp_project, settings)
    result = await backend.run_task(spec)
    assert result.success is False
    assert result.error is not None and "cannot exec" in result.error
    assert state_store.load(spec.id).status is TaskStatus.FAILED


@pytest.mark.asyncio
async def test_subprocess_backend_init_without_session_id_is_ignored(
    tmp_project: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An init message whose session_id is missing/non-string must not crash
    or write a bogus session id."""
    state_store = StateStore(tmp_project / ".claude_runner")
    emitter = EventEmitter(
        events_path=state_store.events_path(), log_dir=state_store.root / "logs", stdout=False
    )
    lines = [
        (json.dumps({"type": "system", "subtype": "init"}) + "\n").encode(),
        (json.dumps({"type": "system", "subtype": "init", "session_id": 42}) + "\n").encode(),
    ]

    async def fake_create(*_args, **_kwargs):
        return _FakeProc(lines, rc=0)

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    backend = SubprocessBackend(state_store=state_store, emitter=emitter)
    spec = _spec(tmp_project, settings)
    await backend.run_task(spec)
    assert state_store.load(spec.id).session_id is None


@pytest.mark.asyncio
async def test_subprocess_backend_line_handler_tolerates_garbage(
    tmp_project: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty lines, non-JSON lines, and JSON arrays should all be ignored safely."""
    state_store = StateStore(tmp_project / ".claude_runner")
    emitter = EventEmitter(
        events_path=state_store.events_path(), log_dir=state_store.root / "logs", stdout=False
    )
    lines = [
        b"\n",  # empty-after-strip
        b"not json at all\n",
        b"[1, 2, 3]\n",  # JSON but not a dict
        (json.dumps({"type": "other"}) + "\n").encode(),  # dict but unrecognized
    ]

    async def fake_create(*_args, **_kwargs):
        return _FakeProc(lines, rc=0)

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    backend = SubprocessBackend(state_store=state_store, emitter=emitter)
    spec = _spec(tmp_project, settings)
    result = await backend.run_task(spec)

    assert result.success is True
    # Usage should remain zero since no recognized usage payloads.
    assert result.usage.input_tokens == 0


@pytest.mark.asyncio
async def test_subprocess_backend_passes_large_stream_read_limit(
    tmp_project: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: ``create_subprocess_exec`` must be called with a large ``limit``.

    A single claude stream-json message can exceed asyncio's default 64 KiB
    readline limit (large tool outputs, long extended-thinking blocks,
    verbose debug payloads). Without a raised limit, ``readline()`` raises
    ``ValueError: Separator is found, but chunk is longer than limit``
    mid-stream and fails the whole task.
    """
    state_store = StateStore(tmp_project / ".claude_runner")
    emitter = EventEmitter(
        events_path=state_store.events_path(), log_dir=state_store.root / "logs", stdout=False
    )

    captured_kwargs: list[dict[str, Any]] = []

    async def fake_create(*_args: str, **kwargs: Any):
        captured_kwargs.append(kwargs)
        return _FakeProc([b""], rc=0)

    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create)

    backend = SubprocessBackend(state_store=state_store, emitter=emitter)
    spec = _spec(tmp_project, settings)
    await backend.run_task(spec)

    assert captured_kwargs, "create_subprocess_exec was not called"
    limit = captured_kwargs[0].get("limit")
    # Must be meaningfully larger than asyncio's 64 KiB default. Pick a
    # threshold (1 MiB) that still fails on the unfixed default but doesn't
    # bind the implementation to an exact byte count.
    assert isinstance(limit, int), (
        "subprocess backend must pass an explicit `limit` to "
        "asyncio.create_subprocess_exec to handle oversize stream-json lines"
    )
    assert limit >= 1 * 1024 * 1024, (
        f"limit={limit} is too small; a single stream-json message can exceed "
        "the asyncio default 64 KiB and cause mid-stream readline() errors"
    )
