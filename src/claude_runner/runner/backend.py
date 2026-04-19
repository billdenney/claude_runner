"""Protocol for runner backends."""

from __future__ import annotations

from typing import Protocol

from claude_runner.models import DispatchResult
from claude_runner.todo.schema import TaskSpec


class RunnerBackend(Protocol):
    name: str

    async def run_task(self, spec: TaskSpec) -> DispatchResult: ...
