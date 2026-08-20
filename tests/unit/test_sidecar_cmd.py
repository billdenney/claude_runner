"""Tests for cli.sidecar_cmd — list / show / answer."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from claude_task_runner.cli.sidecar_cmd import app
from claude_task_runner.queue.schema import (
    SidecarOption,
    SidecarQuestion,
    SidecarRequest,
)
from claude_task_runner.queue.sidecar import (
    request_path,
    response_path,
    sidecar_dir_for,
    write_request,
)
from claude_task_runner.queue.store import queue_runtime_dir


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def queue_dir(tmp_path: Path) -> Path:
    qd = tmp_path / "q"
    qd.mkdir()
    queue_runtime_dir(qd)
    return qd


def _seed_request(
    qd: Path,
    *,
    task_id: str,
    sequence: int = 1,
    summary: str = "Pick the right encoding",
    multi_select: bool = False,
    allow_free_text: bool = False,
) -> SidecarRequest:
    req = SidecarRequest(
        task_id=task_id,
        sequence=sequence,
        created_at=datetime(2026, 5, 4, 12, 0, tzinfo=UTC),
        summary=summary,
        context="Both A and B options visible in source.",
        questions=[
            SidecarQuestion(
                id="encoding",
                prompt="Which encoding?",
                options=[
                    SidecarOption(value="A", label="Encoding A"),
                    SidecarOption(value="B", label="Encoding B", description="alt"),
                ],
                recommended="A",
                multi_select=multi_select,
                allow_free_text=allow_free_text,
            ),
        ],
    )
    write_request(qd, req)
    return req


class TestList:
    def test_empty(self, runner: CliRunner, queue_dir: Path) -> None:
        result = runner.invoke(app, ["list", "--queue", str(queue_dir), "--json"])
        assert result.exit_code == 0
        assert json.loads(result.stdout) == {
            "sidecars": [],
            "n_open": 0,
            "n_outstanding_questions": 0,
        }

    def test_lists_open(self, runner: CliRunner, queue_dir: Path) -> None:
        _seed_request(queue_dir, task_id="001", sequence=1)
        _seed_request(queue_dir, task_id="002", sequence=1)
        result = runner.invoke(app, ["list", "--queue", str(queue_dir), "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        ids = [s["task_id"] for s in payload["sidecars"]]
        assert ids == ["001", "002"]


class TestShow:
    def test_full_payload(self, runner: CliRunner, queue_dir: Path) -> None:
        _seed_request(queue_dir, task_id="001", sequence=1)
        result = runner.invoke(
            app,
            ["show", "001", "1", "--queue", str(queue_dir), "--json"],
        )
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["task_id"] == "001"
        assert payload["questions"][0]["recommended"] == "A"
        assert len(payload["questions"][0]["options"]) == 2

    def test_missing_returns_error(self, runner: CliRunner, queue_dir: Path) -> None:
        result = runner.invoke(
            app,
            ["show", "no-such", "1", "--queue", str(queue_dir), "--json"],
        )
        assert result.exit_code == 1
        assert "not found" in result.stderr or "not found" in result.stdout


class TestAnswer:
    def test_writes_response(self, runner: CliRunner, queue_dir: Path) -> None:
        _seed_request(queue_dir, task_id="001", sequence=1)
        answers = json.dumps([{"id": "encoding", "value": "A"}])
        result = runner.invoke(
            app,
            [
                "answer",
                "001",
                "1",
                "--queue",
                str(queue_dir),
                "--answers",
                answers,
                "--notes",
                "operator chose recommended",
            ],
        )
        assert result.exit_code == 0, result.stdout
        path = response_path(queue_dir, "001", 1)
        assert path.exists()
        body = json.loads(path.read_text())
        assert body["answers"][0]["value"] == "A"
        assert body["notes"] == "operator chose recommended"
        assert body["state"] == "answered"

    def test_multi_select_value_is_list(self, runner: CliRunner, queue_dir: Path) -> None:
        _seed_request(queue_dir, task_id="001", sequence=1, multi_select=True)
        answers = json.dumps([{"id": "encoding", "value": ["A", "B"]}])
        result = runner.invoke(
            app,
            [
                "answer",
                "001",
                "1",
                "--queue",
                str(queue_dir),
                "--answers",
                answers,
            ],
        )
        assert result.exit_code == 0, result.stdout
        body = json.loads(response_path(queue_dir, "001", 1).read_text())
        assert body["answers"][0]["value"] == ["A", "B"]

    def test_answers_from_file(
        self,
        runner: CliRunner,
        queue_dir: Path,
        tmp_path: Path,
    ) -> None:
        _seed_request(queue_dir, task_id="001", sequence=1)
        answers_file = tmp_path / "ans.json"
        answers_file.write_text(json.dumps([{"id": "encoding", "value": "B"}]))
        result = runner.invoke(
            app,
            [
                "answer",
                "001",
                "1",
                "--queue",
                str(queue_dir),
                "--answers-file",
                str(answers_file),
            ],
        )
        assert result.exit_code == 0, result.stdout

    def test_invalid_json_rejected(self, runner: CliRunner, queue_dir: Path) -> None:
        _seed_request(queue_dir, task_id="001", sequence=1)
        result = runner.invoke(
            app,
            [
                "answer",
                "001",
                "1",
                "--queue",
                str(queue_dir),
                "--answers",
                "{not json",
            ],
        )
        assert result.exit_code != 0
        assert "invalid JSON" in (result.stdout + result.stderr)

    def test_must_supply_one_answers_source(self, runner: CliRunner, queue_dir: Path) -> None:
        _seed_request(queue_dir, task_id="001", sequence=1)
        result = runner.invoke(
            app,
            ["answer", "001", "1", "--queue", str(queue_dir)],
        )
        assert result.exit_code != 0
        assert "exactly one" in (result.stdout + result.stderr)

    def test_must_be_array(self, runner: CliRunner, queue_dir: Path) -> None:
        _seed_request(queue_dir, task_id="001", sequence=1)
        result = runner.invoke(
            app,
            [
                "answer",
                "001",
                "1",
                "--queue",
                str(queue_dir),
                "--answers",
                '{"id":"encoding","value":"A"}',
            ],
        )
        assert result.exit_code != 0
        assert "array" in (result.stdout + result.stderr)


def _seed_three_question_request(qd: Path, *, task_id: str = "001", sequence: int = 1) -> None:
    write_request(
        qd,
        SidecarRequest(
            task_id=task_id,
            sequence=sequence,
            created_at=datetime(2026, 8, 9, 9, 0, tzinfo=UTC),
            summary="Three decisions before extraction can continue",
            context="Source is ambiguous in three separate places.",
            questions=[
                SidecarQuestion(
                    id=qid,
                    prompt=f"Decision {qid}?",
                    options=[
                        SidecarOption(value="A", label="Option A"),
                        SidecarOption(value="B", label="Option B"),
                    ],
                    recommended="A",
                )
                for qid in ("q1", "q2", "q3")
            ],
        ),
    )


def _answer(
    runner: CliRunner,
    qd: Path,
    ids: list[str],
    *,
    task_id: str = "001",
    sequence: int = 1,
    extra: list[str] | None = None,
):
    argv = [
        "answer",
        task_id,
        str(sequence),
        "--queue",
        str(qd),
        "--answers",
        json.dumps([{"id": qid, "value": "A"} for qid in ids]),
    ]
    return runner.invoke(app, argv + (extra or []))


class TestListReportsOutstandingQuestions:
    def test_partial_response_is_listed_with_missing_ids(
        self, runner: CliRunner, queue_dir: Path
    ) -> None:
        _seed_three_question_request(queue_dir)
        assert _answer(runner, queue_dir, ["q1"], extra=["--allow-partial"]).exit_code == 0

        result = runner.invoke(app, ["list", "--queue", str(queue_dir), "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["n_open"] == 1
        assert payload["n_outstanding_questions"] == 2
        row = payload["sidecars"][0]
        assert row["task_id"] == "001"
        assert row["outstanding"] == ["q2", "q3"]
        assert row["answered"] == ["q1"]
        assert row["partial"] is True
        # The operator must be able to see WHICH questions are still open.
        assert set(row["prompts"]) == {"q2", "q3"}

    def test_human_output_names_the_outstanding_ids(
        self, runner: CliRunner, queue_dir: Path
    ) -> None:
        _seed_three_question_request(queue_dir)
        assert _answer(runner, queue_dir, ["q1"], extra=["--allow-partial"]).exit_code == 0

        result = runner.invoke(app, ["list", "--queue", str(queue_dir)])
        assert result.exit_code == 0
        assert "q2, q3" in result.stdout
        assert "partial" in result.stdout
        assert "2 unanswered question(s)" in result.stdout

    def test_proposed_names_surface_for_outstanding_questions(
        self, runner: CliRunner, queue_dir: Path
    ) -> None:
        write_request(
            queue_dir,
            SidecarRequest(
                task_id="001",
                sequence=1,
                created_at=datetime(2026, 8, 9, 9, 0, tzinfo=UTC),
                summary="Ratify a canonical covariate name",
                context="Novel covariate encountered.",
                questions=[
                    SidecarQuestion(
                        id="q1",
                        prompt="Which canonical name?",
                        options=[
                            SidecarOption(
                                value="A",
                                label="Adopt `MEAL_INTERVAL`",
                                proposed_names=["MEAL_INTERVAL"],
                            ),
                            SidecarOption(
                                value="B",
                                label="Reuse `FED`",
                                proposed_names=["FED"],
                            ),
                        ],
                    )
                ],
            ),
        )
        result = runner.invoke(app, ["list", "--queue", str(queue_dir), "--json"])
        assert result.exit_code == 0
        row = json.loads(result.stdout)["sidecars"][0]
        assert row["proposed_names"] == ["MEAL_INTERVAL", "FED"]

    def test_unreadable_request_is_listed_not_hidden(
        self, runner: CliRunner, queue_dir: Path
    ) -> None:
        queue_runtime_dir(queue_dir)
        (queue_runtime_dir(queue_dir) / "sidecar" / "001").mkdir(parents=True)
        (queue_runtime_dir(queue_dir) / "sidecar" / "001" / "request-001.json").write_text(
            "{not json"
        )
        result = runner.invoke(app, ["list", "--queue", str(queue_dir), "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["n_open"] == 1
        assert "invalid JSON" in payload["sidecars"][0]["error"]


class TestAnswerRejectsPartialResponses:
    """``sidecar answer`` is where the gap gets created; close it there.

    Counting outstanding questions after the fact only measures the
    backlog. Refusing to write a response that omits an asked question id
    is what stops the backlog growing.
    """

    def test_refuses_to_write_a_partial_response(self, runner: CliRunner, queue_dir: Path) -> None:
        _seed_three_question_request(queue_dir)
        result = _answer(runner, queue_dir, ["q1"])
        assert result.exit_code == 3
        err = result.stdout + result.stderr
        assert "partial response" in err
        assert "q2, q3" in err
        # Nothing is written: the operator retries with a full answer set.
        assert not response_path(queue_dir, "001", 1).exists()

    def test_override_writes_and_leaves_the_rest_open(
        self, runner: CliRunner, queue_dir: Path
    ) -> None:
        _seed_three_question_request(queue_dir)
        result = _answer(runner, queue_dir, ["q1"], extra=["--allow-partial"])
        assert result.exit_code == 0, result.stdout
        body = json.loads(response_path(queue_dir, "001", 1).read_text())
        assert [a["id"] for a in body["answers"]] == ["q1"]

        listing = json.loads(
            runner.invoke(app, ["list", "--queue", str(queue_dir), "--json"]).stdout
        )
        assert listing["sidecars"][0]["outstanding"] == ["q2", "q3"]

    def test_complete_answer_set_is_accepted(self, runner: CliRunner, queue_dir: Path) -> None:
        _seed_three_question_request(queue_dir)
        result = _answer(runner, queue_dir, ["q1", "q2", "q3"])
        assert result.exit_code == 0, result.stdout + result.stderr
        listing = json.loads(
            runner.invoke(app, ["list", "--queue", str(queue_dir), "--json"]).stdout
        )
        assert listing["n_open"] == 0

    def test_answer_order_does_not_matter(self, runner: CliRunner, queue_dir: Path) -> None:
        _seed_three_question_request(queue_dir)
        assert _answer(runner, queue_dir, ["q3", "q1", "q2"]).exit_code == 0

    def test_typo_in_an_answer_id_is_rejected(self, runner: CliRunner, queue_dir: Path) -> None:
        # "q22" does not answer q2; without the gate this wrote a response
        # that silently left q2 open forever.
        _seed_three_question_request(queue_dir)
        result = _answer(runner, queue_dir, ["q1", "q22", "q3"])
        assert result.exit_code == 3
        err = result.stdout + result.stderr
        assert "did not ask: q22" in err
        assert "no answer for q2" in err

    def test_refuses_when_the_request_cannot_be_read(
        self, runner: CliRunner, queue_dir: Path
    ) -> None:
        sidecar_dir_for(queue_dir, "001")
        request_path(queue_dir, "001", 1).write_text("{not json")
        result = _answer(runner, queue_dir, ["q1"])
        assert result.exit_code == 3
        assert "refusing to answer" in (result.stdout + result.stderr)
        assert not response_path(queue_dir, "001", 1).exists()

    def test_refuses_when_the_request_does_not_exist(
        self, runner: CliRunner, queue_dir: Path
    ) -> None:
        result = _answer(runner, queue_dir, ["q1"], task_id="ghost")
        assert result.exit_code == 3
        assert not response_path(queue_dir, "ghost", 1).exists()

    def test_notification_request_accepts_an_empty_answer_set(
        self, runner: CliRunner, queue_dir: Path
    ) -> None:
        write_request(
            queue_dir,
            SidecarRequest(
                task_id="001",
                sequence=1,
                created_at=datetime(2026, 8, 9, 9, 0, tzinfo=UTC),
                summary="Filed for the record",
                context="No decision needed.",
                questions=[],
            ),
        )
        result = _answer(runner, queue_dir, [])
        assert result.exit_code == 0, result.stdout + result.stderr
        listing = json.loads(
            runner.invoke(app, ["list", "--queue", str(queue_dir), "--json"]).stdout
        )
        assert listing["n_open"] == 0

    def test_override_warns_before_dropping_existing_answers(
        self, runner: CliRunner, queue_dir: Path
    ) -> None:
        # answer rewrites the response file wholesale, so a second partial
        # call would discard the first one's answers. Say so.
        _seed_three_question_request(queue_dir)
        assert _answer(runner, queue_dir, ["q1"], extra=["--allow-partial"]).exit_code == 0
        result = _answer(runner, queue_dir, ["q2"], extra=["--allow-partial"])
        assert result.exit_code == 0
        assert "drops existing answers for: q1" in (result.stdout + result.stderr)


class TestAnswerMerge:
    """``--merge`` keeps the completeness gate satisfiable on a backlog.

    56 partially-answered requests already existed when the gate went in.
    Without a way to top one up, clearing it would mean either retyping the
    recorded answers or reaching for ``--allow-partial`` -- which is the
    very thing the gate exists to prevent.
    """

    def test_merge_completes_a_partial_response(self, runner: CliRunner, queue_dir: Path) -> None:
        _seed_three_question_request(queue_dir)
        assert _answer(runner, queue_dir, ["q1"], extra=["--allow-partial"]).exit_code == 0

        result = _answer(runner, queue_dir, ["q2", "q3"], extra=["--merge"])
        assert result.exit_code == 0, result.stdout + result.stderr
        body = json.loads(response_path(queue_dir, "001", 1).read_text())
        assert sorted(a["id"] for a in body["answers"]) == ["q1", "q2", "q3"]

        listing = json.loads(
            runner.invoke(app, ["list", "--queue", str(queue_dir), "--json"]).stdout
        )
        assert listing["n_open"] == 0

    def test_merge_carries_values_verbatim(self, runner: CliRunner, queue_dir: Path) -> None:
        _seed_three_question_request(queue_dir)
        first = runner.invoke(
            app,
            [
                "answer",
                "001",
                "1",
                "--queue",
                str(queue_dir),
                "--allow-partial",
                "--answers",
                json.dumps([{"id": "q1", "value": "B", "notes": "operator rationale"}]),
            ],
        )
        assert first.exit_code == 0, first.stdout + first.stderr

        assert _answer(runner, queue_dir, ["q2", "q3"], extra=["--merge"]).exit_code == 0
        body = json.loads(response_path(queue_dir, "001", 1).read_text())
        carried = next(a for a in body["answers"] if a["id"] == "q1")
        assert carried["value"] == "B"
        assert carried["notes"] == "operator rationale"

    def test_supplied_answer_wins_over_the_recorded_one(
        self, runner: CliRunner, queue_dir: Path
    ) -> None:
        _seed_three_question_request(queue_dir)
        assert _answer(runner, queue_dir, ["q1"], extra=["--allow-partial"]).exit_code == 0
        result = runner.invoke(
            app,
            [
                "answer",
                "001",
                "1",
                "--queue",
                str(queue_dir),
                "--merge",
                "--answers",
                json.dumps([{"id": qid, "value": "B"} for qid in ("q1", "q2", "q3")]),
            ],
        )
        assert result.exit_code == 0, result.stdout + result.stderr
        body = json.loads(response_path(queue_dir, "001", 1).read_text())
        assert [a["value"] for a in body["answers"]] == ["B", "B", "B"]

    def test_merge_is_a_no_op_without_an_existing_response(
        self, runner: CliRunner, queue_dir: Path
    ) -> None:
        _seed_three_question_request(queue_dir)
        result = _answer(runner, queue_dir, ["q1"], extra=["--merge"])
        assert result.exit_code == 3
        assert not response_path(queue_dir, "001", 1).exists()

    def test_rejection_message_points_at_merge(self, runner: CliRunner, queue_dir: Path) -> None:
        _seed_three_question_request(queue_dir)
        assert _answer(runner, queue_dir, ["q1"], extra=["--allow-partial"]).exit_code == 0
        result = _answer(runner, queue_dir, ["q2"])
        assert result.exit_code == 3
        assert "--merge" in (result.stdout + result.stderr)


class TestUnmatchedResponses:
    """A response crediting none of the asked ids is its own fault mode.

    Seen live: a request asking ``extraction_decision`` answered under the
    id ``q1``. Calling that "partial" would misdescribe it, and calling it
    answered would hide it entirely.
    """

    def test_listed_open_with_nothing_answered(self, runner: CliRunner, queue_dir: Path) -> None:
        _seed_three_question_request(queue_dir)
        response_path(queue_dir, "001", 1).write_text(
            json.dumps({"task_id": "001", "answers": [{"id": "typo", "value": "A"}]})
        )
        payload = json.loads(
            runner.invoke(app, ["list", "--queue", str(queue_dir), "--json"]).stdout
        )
        row = payload["sidecars"][0]
        assert row["outstanding"] == ["q1", "q2", "q3"]
        assert row["answered"] == []
        assert row["partial"] is True

    def test_human_output_calls_it_unmatched(self, runner: CliRunner, queue_dir: Path) -> None:
        _seed_three_question_request(queue_dir)
        response_path(queue_dir, "001", 1).write_text(
            json.dumps({"task_id": "001", "answers": [{"id": "typo", "value": "A"}]})
        )
        result = runner.invoke(app, ["list", "--queue", str(queue_dir)])
        assert result.exit_code == 0
        assert "unmatched" in result.stdout
        assert "answers none of the asked ids" in result.stdout


class TestListSummaryIsOneLine:
    def test_long_summary_is_clipped(self, runner: CliRunner, queue_dir: Path) -> None:
        write_request(
            queue_dir,
            SidecarRequest(
                task_id="001",
                sequence=1,
                created_at=datetime(2026, 8, 9, 9, 0, tzinfo=UTC),
                summary="First line of the summary.\nSecond paragraph the listing must not print.",
                context="c",
                questions=[SidecarQuestion(id="q1", prompt="?")],
            ),
        )
        result = runner.invoke(app, ["list", "--queue", str(queue_dir)])
        assert result.exit_code == 0
        assert "First line of the summary." in result.stdout
        assert "Second paragraph" not in result.stdout
        # The full text stays available in the machine-readable views.
        payload = json.loads(
            runner.invoke(app, ["list", "--queue", str(queue_dir), "--json"]).stdout
        )
        assert "Second paragraph" in payload["sidecars"][0]["summary"]
