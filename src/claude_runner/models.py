"""Core dataclasses shared across the package."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class TaskStatus(str, Enum):
    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    BLOCKED = "blocked"


class StopReason(str, Enum):
    END_TURN = "end_turn"
    ERROR = "error"
    MAX_TURNS = "max_turns"
    INTERRUPTED = "interrupted"


@dataclass(slots=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def uncached_input(self) -> int:
        return max(self.input_tokens - self.cache_read_tokens - self.cache_creation_tokens, 0)

    @property
    def billable_total(self) -> int:
        """Tokens that count against rate-limit windows (excludes cache reads)."""
        return self.uncached_input + self.output_tokens + self.cache_creation_tokens


@dataclass(slots=True)
class RunRecord:
    attempt: int
    started_at: datetime
    finished_at: datetime | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)
    stop_reason: StopReason | None = None
    error: str | None = None

    @property
    def duration_s(self) -> float:
        if self.finished_at is None:
            return 0.0
        return (self.finished_at - self.started_at).total_seconds()


@dataclass(slots=True)
class TaskState:
    """Machine-written state, persisted under .claude_runner/state/<id>.yaml."""

    task_id: str
    status: TaskStatus = TaskStatus.PENDING
    session_id: str | None = None
    attempts: int = 0
    last_started_at: datetime | None = None
    last_finished_at: datetime | None = None
    stop_reason: StopReason | None = None
    runs: list[RunRecord] = field(default_factory=list)
    error: str | None = None

    def is_terminal(self) -> bool:
        return self.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.BLOCKED}

    def is_in_flight(self) -> bool:
        return self.status in {TaskStatus.RUNNING, TaskStatus.QUEUED}

    def needs_resume(self) -> bool:
        return (
            self.status in {TaskStatus.RUNNING, TaskStatus.INTERRUPTED}
            and self.session_id is not None
        )


@dataclass(slots=True)
class DispatchResult:
    """Returned by a backend after a task run completes (success or failure)."""

    task_id: str
    success: bool
    usage: TokenUsage
    stop_reason: StopReason
    session_id: str | None
    duration_s: float
    error: str | None = None
