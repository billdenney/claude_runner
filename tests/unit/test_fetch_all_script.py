"""Syntax and argument handling of the runner-answer-sidecar ``fetch_all.sh``.

The skill tells agents to run ``bash fetch_all.sh --queue <dir>``. The script
takes no other argument, has no ``--help``, and hands the queue to
``claude-task-runner sidecar list`` before a Python heredoc reshapes the
listing. Only syntax and argument handling are pinned here: the CLI is a stub
that records how it was called, so nothing depends on what the real CLI makes
of the queue.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from claude_task_runner.cli.install_skills_cmd import _packaged_skill_dir
from claude_task_runner.queue.store import queue_runtime_dir, todo_dir

FETCH_ALL = _packaged_skill_dir("runner-answer-sidecar") / "fetch_all.sh"

STUB = """\
#!/bin/bash
printf '%s\\n' "$@" -- >> "$STUB_LOG"
echo '{"sidecars": [], "n_open": 0, "n_outstanding_questions": 0}'
"""


@pytest.fixture
def stub_env(tmp_path: Path) -> dict[str, str]:
    """An environment whose ``claude-task-runner`` logs each call's argv to STUB_LOG."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "claude-task-runner"
    stub.write_text(STUB)
    stub.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "STUB_LOG": str(tmp_path / "calls.log"),
    }


@pytest.fixture
def queue(tmp_path: Path) -> Path:
    queue = tmp_path / "queue"
    queue.mkdir()
    todo_dir(queue)
    queue_runtime_dir(queue)
    return queue


def _run(*args: str, env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(FETCH_ALL), *args],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


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


def test_unknown_argument_exits_2_before_calling_the_cli(
    stub_env: dict[str, str], tmp_path: Path
) -> None:
    proc = _run("--no-such-flag", env=stub_env, cwd=tmp_path)
    assert (proc.returncode, proc.stdout, proc.stderr) == (2, "", "unknown arg: --no-such-flag\n")
    assert not Path(stub_env["STUB_LOG"]).exists()


@pytest.mark.parametrize("pass_queue", [True, False], ids=["--queue", "default-cwd"])
def test_queue_reaches_sidecar_list(
    pass_queue: bool, stub_env: dict[str, str], queue: Path, tmp_path: Path
) -> None:
    """``--queue`` names the queue; without it, the working directory is the queue."""
    args = ["--queue", str(queue)] if pass_queue else []
    proc = _run(*args, env=stub_env, cwd=tmp_path if pass_queue else queue)
    assert (proc.returncode, proc.stderr) == (0, "")
    calls = Path(stub_env["STUB_LOG"]).read_text()
    assert calls == f"sidecar\nlist\n--queue\n{queue}\n--json\n--\n"
    assert json.loads(proc.stdout) == {
        "queue": str(queue),
        "n_open": 0,
        "n_outstanding_questions": 0,
        "sidecars": [],
    }
