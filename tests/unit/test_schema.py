"""Tests for queue/schema.py — pydantic models for v2 task queue."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from claude_task_runner.queue.schema import (
    CURRENT_SCHEMA_VERSION,
    RunRecord,
    SidecarAnswer,
    SidecarOption,
    SidecarQuestion,
    SidecarRequest,
    SidecarResponse,
    Task,
    TaskState,
    TokenUsage,
)


class TestTokenUsage:
    def test_defaults_zero(self) -> None:
        u = TokenUsage()
        assert u.total_tokens == 0

    def test_total_sums_all(self) -> None:
        u = TokenUsage(
            input_tokens=100,
            output_tokens=200,
            cache_read_tokens=300,
            cache_creation_tokens=50,
        )
        assert u.total_tokens == 650

    def test_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TokenUsage(input_tokens=-1)


class TestTask:
    def test_minimal_valid(self) -> None:
        t = Task(id="001-foo", title="Foo", prompt="do foo")
        assert t.schema_version == CURRENT_SCHEMA_VERSION
        assert t.model == "claude-opus-4-7"
        assert t.effort == "medium"
        assert t.priority == "normal"
        assert t.allowed_tools == []
        assert t.depends_on == []
        assert t.tags == []
        assert t.weekly_critical is False
        assert t.weekly_deferrable is False
        assert t.force_dispatch_in_eow is False

    def test_empty_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Task(id="", title="Foo", prompt="do foo")

    def test_unknown_priority_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Task(id="x", title="t", prompt="p", priority="urgent")  # type: ignore[arg-type]

    def test_unknown_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Task.model_validate({"id": "x", "title": "t", "prompt": "p", "bogus_field": 1})

    def test_max_tokens_override(self) -> None:
        t = Task(id="x", title="t", prompt="p", max_tokens_override=5_000_000)
        assert t.max_tokens_override == 5_000_000

    def test_max_tokens_override_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Task(id="x", title="t", prompt="p", max_tokens_override=0)

    def test_tags_round_trip(self) -> None:
        t = Task(id="x", title="t", prompt="p", tags=["paper", "popPK"])
        assert t.tags == ["paper", "popPK"]

    def test_effort_is_free_string(self) -> None:
        # Validation against per-model accepted set is in runner.effort_levels;
        # at the schema layer effort accepts any non-empty string.
        t = Task(id="x", title="t", prompt="p", effort="extra_high")
        assert t.effort == "extra_high"

    def test_effort_empty_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Task(id="x", title="t", prompt="p", effort="")


class TestRunRecord:
    def test_minimal(self) -> None:
        t = datetime(2026, 5, 3, 18, 0, tzinfo=UTC)
        r = RunRecord(
            attempt=1,
            started_at=t,
            finished_at=t,
            stop_reason="end_turn",
            duration_s=0.5,
        )
        assert r.attempt == 1
        assert r.error is None
        assert r.killed_by_cap is None
        assert r.usage.total_tokens == 0

    def test_attempt_zero_rejected(self) -> None:
        t = datetime(2026, 5, 3, 18, 0, tzinfo=UTC)
        with pytest.raises(ValidationError):
            RunRecord(
                attempt=0,
                started_at=t,
                finished_at=t,
                stop_reason="end_turn",
                duration_s=0,
            )

    def test_killed_by_cap_literal(self) -> None:
        t = datetime(2026, 5, 3, 18, 0, tzinfo=UTC)
        r = RunRecord(
            attempt=2,
            started_at=t,
            finished_at=t,
            stop_reason="killed",
            duration_s=10,
            killed_by_cap="tokens",
        )
        assert r.killed_by_cap == "tokens"
        with pytest.raises(ValidationError):
            RunRecord(
                attempt=2,
                started_at=t,
                finished_at=t,
                stop_reason="killed",
                duration_s=10,
                killed_by_cap="memory",  # type: ignore[arg-type]
            )

    def test_pid_field_defaults_to_none(self) -> None:
        """Legacy run records that pre-date the pid field round-trip cleanly."""
        t = datetime(2026, 5, 3, 18, 0, tzinfo=UTC)
        r = RunRecord(
            attempt=1,
            started_at=t,
            finished_at=t,
            stop_reason="end_turn",
            duration_s=1,
        )
        assert r.pid is None

    def test_pid_field_accepts_positive_int(self) -> None:
        """The subprocess pid is recorded so the orchestrator can probe
        liveness post-finalize for subprocess-leak detection."""
        t = datetime(2026, 5, 3, 18, 0, tzinfo=UTC)
        r = RunRecord(
            attempt=1,
            started_at=t,
            finished_at=t,
            stop_reason="killed_by_cap",
            duration_s=1,
            pid=4242,
        )
        assert r.pid == 4242

    def test_pid_field_rejects_non_positive(self) -> None:
        """pid is constrained to ``ge=1`` (real Linux pids start at 1)."""
        t = datetime(2026, 5, 3, 18, 0, tzinfo=UTC)
        with pytest.raises(ValidationError):
            RunRecord(
                attempt=1,
                started_at=t,
                finished_at=t,
                stop_reason="end_turn",
                duration_s=1,
                pid=0,
            )


class TestTaskState:
    def test_defaults(self) -> None:
        s = TaskState(task_id="001-foo")
        assert s.status == "pending"
        assert s.attempts == 0
        assert s.resume_attempts == 0
        assert s.session_id is None
        assert s.runs == []

    def test_status_enum_enforced(self) -> None:
        with pytest.raises(ValidationError):
            TaskState(task_id="x", status="bogus")  # type: ignore[arg-type]

    def test_with_run(self) -> None:
        t = datetime(2026, 5, 3, 18, 0, tzinfo=UTC)
        run = RunRecord(
            attempt=1,
            started_at=t,
            finished_at=t,
            stop_reason="end_turn",
            duration_s=1,
        )
        s = TaskState(task_id="x", status="completed", attempts=1, runs=[run])
        assert len(s.runs) == 1
        assert s.runs[0].stop_reason == "end_turn"

    def test_session_account_field_optional(self) -> None:
        s = TaskState(task_id="x")
        assert s.session_account is None

    def test_session_host_account_no_session_returns_none(self) -> None:
        s = TaskState(task_id="x", session_account="work")
        assert s.session_host_account() is None

    def test_session_host_account_explicit_field_wins(self) -> None:
        s = TaskState(task_id="x", session_id="sess1", session_account="work")
        assert s.session_host_account() == "work"

    def test_session_host_account_falls_back_to_last_run(self) -> None:
        """Legacy state YAML without session_account field — derive from the
        most recent attempt's account. This is the backwards-compatibility
        path for state YAMLs written before ADR-0024."""
        t = datetime(2026, 5, 3, 18, 0, tzinfo=UTC)
        runs = [
            RunRecord(
                attempt=1,
                started_at=t,
                finished_at=t,
                stop_reason="end_turn",
                duration_s=1,
                account="personal",
            ),
            RunRecord(
                attempt=2,
                started_at=t,
                finished_at=t,
                stop_reason="end_turn",
                duration_s=1,
                account="work",
            ),
        ]
        s = TaskState(task_id="x", session_id="sess1", runs=runs)
        assert s.session_host_account() == "work"

    def test_session_host_account_skips_runs_without_account(self) -> None:
        """A legacy single-account run (account=None) is skipped to find the
        most recent run that did record an account."""
        t = datetime(2026, 5, 3, 18, 0, tzinfo=UTC)
        runs = [
            RunRecord(
                attempt=1,
                started_at=t,
                finished_at=t,
                stop_reason="end_turn",
                duration_s=1,
                account="personal",
            ),
            RunRecord(
                attempt=2,
                started_at=t,
                finished_at=t,
                stop_reason="end_turn",
                duration_s=1,
                account=None,
            ),
        ]
        s = TaskState(task_id="x", session_id="sess1", runs=runs)
        assert s.session_host_account() == "personal"

    def test_session_host_account_session_but_no_account_anywhere(self) -> None:
        """A session_id with no derivable host returns None — the dispatch
        policy treats this as "no affinity constraint" (legacy single-
        account queues; safe to dispatch on any account)."""
        t = datetime(2026, 5, 3, 18, 0, tzinfo=UTC)
        runs = [
            RunRecord(
                attempt=1,
                started_at=t,
                finished_at=t,
                stop_reason="end_turn",
                duration_s=1,
                account=None,
            ),
        ]
        s = TaskState(task_id="x", session_id="sess1", runs=runs)
        assert s.session_host_account() is None


class TestSidecar:
    def _question(self) -> SidecarQuestion:
        return SidecarQuestion(
            id="q1",
            prompt="Pick one",
            options=[
                SidecarOption(value="A", label="Option A"),
                SidecarOption(value="B", label="Option B"),
            ],
            recommended="A",
        )

    def test_request_minimal(self) -> None:
        t = datetime(2026, 5, 3, 18, 0, tzinfo=UTC)
        req = SidecarRequest(
            task_id="001",
            sequence=1,
            created_at=t,
            summary="ambiguous something",
            context="full context here",
            questions=[self._question()],
        )
        assert req.state == "pending"
        assert req.questions[0].recommended == "A"
        assert req.questions[0].allow_free_text is False

    def test_response_state_must_be_answered(self) -> None:
        t = datetime(2026, 5, 3, 18, 0, tzinfo=UTC)
        with pytest.raises(ValidationError):
            SidecarResponse(
                task_id="001",
                sequence=1,
                responded_at=t,
                state="pending",  # type: ignore[arg-type]
                answers=[SidecarAnswer(id="q1", value="A")],
            )

    def test_response_default_notes_empty(self) -> None:
        t = datetime(2026, 5, 3, 18, 0, tzinfo=UTC)
        resp = SidecarResponse(
            task_id="001",
            sequence=1,
            responded_at=t,
            answers=[SidecarAnswer(id="q1", value="A")],
        )
        assert resp.notes == ""

    def test_multi_select_answer(self) -> None:
        t = datetime(2026, 5, 3, 18, 0, tzinfo=UTC)
        resp = SidecarResponse(
            task_id="001",
            sequence=1,
            responded_at=t,
            answers=[SidecarAnswer(id="q1", value=["A", "B"])],
        )
        assert resp.answers[0].value == ["A", "B"]

    def test_zero_sequence_rejected(self) -> None:
        t = datetime(2026, 5, 3, 18, 0, tzinfo=UTC)
        with pytest.raises(ValidationError):
            SidecarRequest(
                task_id="x",
                sequence=0,
                created_at=t,
                summary="s",
                context="c",
                questions=[self._question()],
            )
