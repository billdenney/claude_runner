"""The runner-status ``snapshot.sh`` refuses a directory that is not a queue,
and never reports sidecars it could not list as "no open sidecars".

The per-account table is pinned in ``test_snapshot_per_account.py``. This
file pins the rest of the script's contract:

* The queue check. A missing path, a directory without ``todo/`` (named by
  ``--queue`` or, without it, the working directory) and a ``todo`` that is a
  file each exit 2 before anything is printed, before the CLI runs, and
  without touching the directory. The script used to report each of them as
  an idle, empty queue and exit 0, and its ``sidecar list`` call created
  ``.claude_task_runner/`` in a directory that was not a queue.
* The report for a queue whose supervisor has never run, line for line.
* The open-sidecars section. The real CLI lists an empty queue and an open
  request. A ``claude-task-runner`` that exits non-zero, prints something
  other than a listing, or is missing from ``PATH`` gives "could not list"
  and exit 1, never "(none)".

Every run gets a ``PATH`` of its own from ``_cli_on_path``, so which
``claude-task-runner`` is installed on the machine makes no difference. The
real CLI runs through a shim that imports the package under test with this
interpreter.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from claude_task_runner.cli.install_skills_cmd import _packaged_skill_dir
from claude_task_runner.queue.schema import SidecarOption, SidecarQuestion, SidecarRequest

from ._cli_on_path import (
    LISTING_FAILURES,
    make_bin,
    real_cli,
    run_script,
    sidecar_list_call,
    stub_cli,
)
from ._sidecar_files import write_request

SNAPSHOT = _packaged_skill_dir("runner-status") / "snapshot.sh"

INCOMPLETE = "could not list the open sidecars, so the report is incomplete\n"
"""What the script prints on stderr, after the report, when it exits 1."""


@pytest.fixture
def bin_dir(tmp_path: Path) -> Path:
    return make_bin(tmp_path)


@pytest.fixture
def queue(tmp_path: Path) -> Path:
    """A queue as setup leaves it: the directory and its ``todo/``."""
    queue = tmp_path / "queue"
    (queue / "todo").mkdir(parents=True)
    return queue


def _run(bin_dir: Path, *args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return run_script(SNAPSHOT, bin_dir, *args, cwd=cwd)


# ---------------------------------------------------------------------------
# The queue check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["missing", ""], ids=["missing-path", "empty-string"])
def test_missing_queue_exits_2_before_anything_runs(
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
    assert not (tmp_path / "missing").exists()


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text("not a queue\n")
    return project


def _make_todo_file(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "todo").write_text("")
    return project


@pytest.mark.parametrize(
    "make", [_make_project, _make_todo_file], ids=["no-todo", "todo-is-a-file"]
)
@pytest.mark.parametrize("pass_queue", [True, False], ids=["--queue", "working-directory"])
def test_directory_that_is_not_a_queue_exits_2_and_is_left_alone(
    make: Callable[[Path], Path], pass_queue: bool, tmp_path: Path, bin_dir: Path
) -> None:
    """Without the check, the real ``sidecar list`` would create
    ``.claude_task_runner/`` in the directory, and the report would call it
    an idle, empty queue."""
    project = make(tmp_path)
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
# A queue whose supervisor has never run
# ---------------------------------------------------------------------------

FRESH_QUEUE_REPORT = """
**Supervisor**: NOT RUNNING (no pidfile at {runtime}/supervisor.pid)

**supervisor.json**: missing at {runtime}/supervisor.json


**Queue counts**

| field | value |
|---|---|
| state.completed | 0 |
| state.failed | 0 |
| state.running | 0 |
| state.awaiting_sidecar | 0 |
| state.possibly_hung | 0 |
| state.failed_circuit_breaker | 0 |
| state files (total) | 0 |
| todo/*.yaml | 2 |

**Open sidecars**: 0 request(s), 0 unanswered question(s)

(none)

"""


@pytest.mark.parametrize("pass_queue", [True, False], ids=["--queue", "working-directory"])
def test_fresh_queue_gets_the_whole_report(pass_queue: bool, queue: Path, bin_dir: Path) -> None:
    """A queue that has only ``todo/`` is still a queue. Its **Queue counts**
    table used to lose its header, leaving the ``todo/*.yaml`` row alone,
    because the header was printed only when the state directory existed."""
    for task_id in ("t-001", "t-002"):
        (queue / "todo" / f"{task_id}.yaml").write_text(f"id: {task_id}\n")
    (queue / "todo" / "notes.txt").write_text("not a task\n")
    log = real_cli(bin_dir)
    args = ["--queue", str(queue)] if pass_queue else []
    proc = _run(bin_dir, *args, cwd=queue.parent if pass_queue else queue)
    assert (proc.returncode, proc.stderr) == (0, "")
    stamp, report = proc.stdout.split("\n", 1)
    assert re.fullmatch(r"## Queue status — \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", stamp)
    assert report == FRESH_QUEUE_REPORT.format(runtime=queue / ".claude_task_runner")
    assert log.read_text() == sidecar_list_call(queue)


# ---------------------------------------------------------------------------
# Open sidecars
# ---------------------------------------------------------------------------


def test_open_request_is_listed_with_its_outstanding_question(queue: Path, bin_dir: Path) -> None:
    """Pins the JSON contract between the script and the real ``sidecar list``."""
    write_request(
        queue,
        SidecarRequest(
            task_id="t-001",
            sequence=1,
            created_at=datetime(2026, 9, 26, 12, 0, tzinfo=UTC),
            summary="Pick the right encoding",
            context="Both encodings appear in the source.",
            questions=[
                SidecarQuestion(
                    id="encoding",
                    prompt="Which encoding?",
                    options=[
                        SidecarOption(value="A", label="Encoding A"),
                        SidecarOption(value="B", label="Encoding B"),
                    ],
                )
            ],
        ),
    )
    log = real_cli(bin_dir)
    proc = _run(bin_dir, "--queue", str(queue), cwd=queue.parent)
    assert (proc.returncode, proc.stderr) == (0, "")
    assert proc.stdout.endswith(
        "\n**Open sidecars**: 1 request(s), 1 unanswered question(s)\n"
        "\n"
        "| task_id | sequence | outstanding | state |\n"
        "|---|---|---|---|\n"
        "| t-001 | 1 | encoding | unanswered |\n"
        "\n"
    )
    assert log.read_text() == sidecar_list_call(queue)


@pytest.mark.parametrize(("stdout", "stderr", "code", "why", "shown"), LISTING_FAILURES)
def test_sidecars_that_could_not_be_listed_are_never_reported_as_none(
    stdout: str,
    stderr: str,
    code: int,
    why: str,
    shown: str,
    queue: Path,
    bin_dir: Path,
) -> None:
    """A failed ``sidecar list`` used to be replaced with an empty listing,
    which printed "(none)" and exited 0."""
    log = stub_cli(bin_dir, stdout=stdout, stderr=stderr, code=code)
    proc = _run(bin_dir, "--queue", str(queue), cwd=queue.parent)
    assert (proc.returncode, proc.stderr) == (1, INCOMPLETE)
    head, section = proc.stdout.split("**Open sidecars**", 1)
    assert section == f": could not list: {why}\n\n```\n{shown}\n```\n\n"
    # The report before the section is complete.
    assert head.endswith("| todo/*.yaml | 0 |\n\n")
    assert log.read_text() == sidecar_list_call(queue)


def test_cli_missing_from_path_is_could_not_list(queue: Path, bin_dir: Path) -> None:
    proc = _run(bin_dir, "--queue", str(queue), cwd=queue.parent)
    assert (proc.returncode, proc.stderr) == (1, INCOMPLETE)
    assert proc.stdout.endswith(
        "| todo/*.yaml | 0 |\n"
        "\n"
        "**Open sidecars**: could not list: claude-task-runner is not on PATH\n"
        "\n"
    )
