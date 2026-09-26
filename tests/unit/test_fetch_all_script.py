"""Syntax, argument handling and failure reporting of the runner-answer-sidecar
``fetch_all.sh``.

The skill tells agents to run ``bash fetch_all.sh --queue <dir>``. The script
takes no other argument, has no ``--help``, and hands the queue to
``claude-task-runner sidecar list`` before a Python heredoc reshapes the
listing. The syntax and argument-handling tests use a stub CLI that records
how it was called, so nothing depends on what the real CLI makes of the
queue.

The rest pin that the script never reports sidecars it could not list as
none. A missing path, or a directory without ``todo/``, exits 2 before the
CLI runs. A CLI missing from ``PATH``, a failed ``sidecar list``, or output
that is not a listing exits 1 with the reason on stderr and nothing on
stdout. Before, a directory that was not a queue came back as
``"n_open": 0``, which the skill reports as "No open sidecars", and a failed
listing ended the script with the CLI's exit code and no message. One test
runs the real CLI against a partly answered request. Every run gets a
``PATH`` of its own from ``_cli_on_path``.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_task_runner.cli.install_skills_cmd import _packaged_skill_dir
from claude_task_runner.queue.schema import (
    SidecarAnswer,
    SidecarOption,
    SidecarQuestion,
    SidecarRequest,
    SidecarResponse,
)
from claude_task_runner.queue.sidecar import write_response
from claude_task_runner.queue.store import queue_runtime_dir, todo_dir

from ._cli_on_path import (
    EMPTY_LISTING,
    LISTING_FAILURES,
    make_bin,
    real_cli,
    run_script,
    sidecar_list_call,
    stub_cli,
)
from ._sidecar_files import write_request

FETCH_ALL = _packaged_skill_dir("runner-answer-sidecar") / "fetch_all.sh"


@pytest.fixture
def bin_dir(tmp_path: Path) -> Path:
    return make_bin(tmp_path)


@pytest.fixture
def queue(tmp_path: Path) -> Path:
    queue = tmp_path / "queue"
    queue.mkdir()
    todo_dir(queue)
    queue_runtime_dir(queue)
    return queue


def _run(bin_dir: Path, *args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return run_script(FETCH_ALL, bin_dir, *args, cwd=cwd)


def test_parses() -> None:
    proc = subprocess.run(
        ["bash", "-n", str(FETCH_ALL)], capture_output=True, text=True, timeout=60, check=False
    )
    assert (proc.returncode, proc.stderr) == (0, "")


def test_embedded_python_compiles() -> None:
    """``bash -n`` does not look inside the heredoc, so compile it separately."""
    heredoc = re.search(r"python3 - <<'EOF'\n(.*?)^EOF$", FETCH_ALL.read_text(), re.S | re.M)
    assert heredoc is not None, "fetch_all.sh no longer pipes a python3 heredoc"
    compile(heredoc.group(1), f"{FETCH_ALL}:heredoc", "exec")


def test_unknown_argument_exits_2_before_calling_the_cli(bin_dir: Path, tmp_path: Path) -> None:
    log = stub_cli(bin_dir, stdout=EMPTY_LISTING)
    proc = _run(bin_dir, "--no-such-flag", cwd=tmp_path)
    assert (proc.returncode, proc.stdout, proc.stderr) == (2, "", "unknown arg: --no-such-flag\n")
    assert not log.exists()


@pytest.mark.parametrize("pass_queue", [True, False], ids=["--queue", "default-cwd"])
def test_queue_reaches_sidecar_list(
    pass_queue: bool, bin_dir: Path, queue: Path, tmp_path: Path
) -> None:
    """``--queue`` names the queue; without it, the working directory is the queue."""
    log = stub_cli(bin_dir, stdout=EMPTY_LISTING)
    args = ["--queue", str(queue)] if pass_queue else []
    proc = _run(bin_dir, *args, cwd=tmp_path if pass_queue else queue)
    assert (proc.returncode, proc.stderr) == (0, "")
    assert log.read_text() == sidecar_list_call(queue)
    assert json.loads(proc.stdout) == {
        "queue": str(queue),
        "n_open": 0,
        "n_outstanding_questions": 0,
        "sidecars": [],
    }


# ---------------------------------------------------------------------------
# A queue that is not one
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["missing", ""], ids=["missing-path", "empty-string"])
def test_missing_queue_exits_2_before_the_cli_runs(
    name: str, tmp_path: Path, bin_dir: Path
) -> None:
    log = real_cli(bin_dir)
    arg = str(tmp_path / name) if name else ""
    proc = _run(bin_dir, "--queue", arg, cwd=tmp_path)
    assert (proc.returncode, proc.stdout, proc.stderr) == (
        2,
        "",
        f"--queue is not an existing directory: {arg}\n",
    )
    assert not log.exists()


@pytest.mark.parametrize("todo_is_a_file", [False, True], ids=["no-todo", "todo-is-a-file"])
@pytest.mark.parametrize("pass_queue", [True, False], ids=["--queue", "working-directory"])
def test_directory_that_is_not_a_queue_exits_2_and_is_left_alone(
    todo_is_a_file: bool, pass_queue: bool, tmp_path: Path, bin_dir: Path
) -> None:
    """Without the check, the real ``sidecar list`` would create
    ``.claude_task_runner/`` in the directory and list nothing, which the
    skill reports as "No open sidecars"."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("not a queue\n")
    if todo_is_a_file:
        (project / "todo").write_text("")
    before = sorted(p.name for p in project.iterdir())
    log = real_cli(bin_dir)
    args = ["--queue", str(project)] if pass_queue else []
    named = "--queue" if pass_queue else "the working directory (no --queue given)"
    proc = _run(bin_dir, *args, cwd=tmp_path if pass_queue else project)
    assert (proc.returncode, proc.stdout, proc.stderr) == (
        2,
        "",
        f"{named} is not a queue directory, it has no todo/ subdirectory: {project}\n",
    )
    assert not log.exists()
    assert sorted(p.name for p in project.iterdir()) == before


# ---------------------------------------------------------------------------
# A listing that failed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("stdout", "stderr", "code", "why", "shown"), LISTING_FAILURES)
def test_sidecars_that_could_not_be_listed_exit_1(
    stdout: str,
    stderr: str,
    code: int,
    why: str,
    shown: str,
    queue: Path,
    bin_dir: Path,
) -> None:
    """Never ``"n_open": 0``: nothing on stdout, and the reason on stderr."""
    log = stub_cli(bin_dir, stdout=stdout, stderr=stderr, code=code)
    proc = _run(bin_dir, "--queue", str(queue), cwd=queue.parent)
    assert (proc.returncode, proc.stdout, proc.stderr) == (
        1,
        "",
        f"could not list the open sidecars: {why}\n{shown}\n",
    )
    assert log.read_text() == sidecar_list_call(queue)


def test_cli_missing_from_path_exits_1(queue: Path, bin_dir: Path) -> None:
    proc = _run(bin_dir, "--queue", str(queue), cwd=queue.parent)
    assert (proc.returncode, proc.stdout, proc.stderr) == (
        1,
        "",
        "could not list the open sidecars: claude-task-runner is not on PATH\n",
    )


# ---------------------------------------------------------------------------
# The real CLI
# ---------------------------------------------------------------------------


def test_partly_answered_request_returns_only_its_outstanding_question(
    queue: Path, bin_dir: Path
) -> None:
    """``sidecar list`` says which questions are outstanding, ``sidecar show``
    supplies them, and a question already answered is left out."""
    request = SidecarRequest(
        task_id="t-001",
        sequence=1,
        created_at=datetime(2026, 9, 26, 12, 0, tzinfo=UTC),
        summary="Pick the covariate encodings",
        context="Both encodings appear in the source.",
        questions=[
            SidecarQuestion(
                id="sex",
                prompt="How is sex coded?",
                options=[
                    SidecarOption(value="A", label="0 = male"),
                    SidecarOption(value="B", label="0 = female"),
                ],
                recommended="A",
            ),
            SidecarQuestion(id="race", prompt="How is race coded?", allow_free_text=True),
        ],
    )
    write_request(queue, request)
    response = write_response(
        queue,
        SidecarResponse(
            task_id="t-001",
            sequence=1,
            responded_at=datetime(2026, 9, 26, 13, 0, tzinfo=UTC),
            answers=[SidecarAnswer(id="sex", value="A")],
        ),
    )
    log = real_cli(bin_dir)
    proc = _run(bin_dir, "--queue", str(queue), cwd=queue.parent)
    assert (proc.returncode, proc.stderr) == (0, "")
    assert json.loads(proc.stdout) == {
        "queue": str(queue),
        "n_open": 1,
        "n_outstanding_questions": 1,
        "sidecars": [
            {
                "task_id": "t-001",
                "sequence": 1,
                "summary": "Pick the covariate encodings",
                "context": "Both encodings appear in the source.",
                "questions": [request.model_dump(mode="json")["questions"][1]],
                "outstanding": ["race"],
                "answered": ["sex"],
                "partial": True,
                "response_path": str(response),
            }
        ],
    }
    assert log.read_text() == (
        sidecar_list_call(queue) + f"sidecar\nshow\nt-001\n1\n--queue\n{queue}\n--json\n--\n"
    )
