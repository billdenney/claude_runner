"""Run a skill's shell script with the ``claude-task-runner`` a test chooses.

The runner-status and runner-answer-sidecar scripts call
``claude-task-runner`` from ``PATH`` and run their Python heredocs with
``python3``. :func:`run_script` puts a ``bin`` directory of the test's first
on ``PATH`` and drops every other directory holding a
``claude-task-runner``, so which one, if any, is installed on the machine
makes no difference. :func:`make_bin` creates that ``bin`` with a
``python3`` that runs this interpreter. :func:`real_cli` and
:func:`stub_cli` put a ``claude-task-runner`` in it. Both log each call's
argv, one argument per line followed by ``--``.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest


def _write_exe(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def make_bin(tmp_path: Path) -> Path:
    """Create ``tmp_path/bin`` holding a ``python3`` that runs this interpreter."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_exe(bin_dir / "python3", f'exec {shlex.quote(sys.executable)} "$@"\n')
    return bin_dir


def _log_calls(bin_dir: Path) -> tuple[Path, str]:
    """Return the call log's path and a shell line that appends ``$@`` to it."""
    log = bin_dir.parent / "calls.log"
    return log, f'printf "%s\\n" "$@" -- >> {shlex.quote(str(log))}\n'


def real_cli(bin_dir: Path) -> Path:
    """Install a ``claude-task-runner`` that logs its argv, then runs the
    package under test with this interpreter. Return the call log's path."""
    log, record = _log_calls(bin_dir)
    run = "from claude_task_runner.cli import main; main()"
    _write_exe(
        bin_dir / "claude-task-runner",
        record + f'exec {shlex.quote(sys.executable)} -c "{run}" "$@"\n',
    )
    return log


def stub_cli(bin_dir: Path, *, stdout: str = "", stderr: str = "", code: int = 0) -> Path:
    """Install a ``claude-task-runner`` that logs its argv, prints ``stdout``
    and ``stderr`` and exits ``code``. Return the call log's path."""
    log, record = _log_calls(bin_dir)
    _write_exe(
        bin_dir / "claude-task-runner",
        record
        + f"printf %s {shlex.quote(stdout)}\n"
        + f"printf %s {shlex.quote(stderr)} >&2\n"
        + f"exit {code}\n",
    )
    return log


def run_script(
    script: Path, bin_dir: Path, *args: str, cwd: Path
) -> subprocess.CompletedProcess[str]:
    """Run ``bash script *args`` in ``cwd`` with ``bin_dir`` first on ``PATH``."""
    kept = [
        d
        for d in os.environ["PATH"].split(os.pathsep)
        if d and not (Path(d) / "claude-task-runner").exists()
    ]
    env = {**os.environ, "PATH": os.pathsep.join([str(bin_dir), *kept]), "PWD": str(cwd)}
    return subprocess.run(
        ["bash", str(script), *args],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


EMPTY_LISTING = '{"sidecars": [], "n_open": 0, "n_outstanding_questions": 0}\n'
"""What ``sidecar list --json`` prints for a queue with no open sidecars."""


def sidecar_list_call(queue: Path) -> str:
    """The call log after one ``sidecar list --queue <queue> --json``."""
    return f"sidecar\nlist\n--queue\n{queue}\n--json\n--\n"


_LIST = "`claude-task-runner sidecar list --json`"
_JSON_ERROR = '{"ok": false, "error": "--queue is not an existing directory: /gone"}'
_TRACEBACK = "Traceback (most recent call last):\nRuntimeError: boom"

LISTING_FAILURES = [
    pytest.param("", _TRACEBACK + "\n", 3, f"{_LIST} exited 3", _TRACEBACK, id="crash"),
    pytest.param(_JSON_ERROR + "\n", "", 2, f"{_LIST} exited 2", _JSON_ERROR, id="json-error"),
    pytest.param(
        _JSON_ERROR + "\n",
        _TRACEBACK + "\n",
        2,
        f"{_LIST} exited 2",
        f"{_JSON_ERROR}\n{_TRACEBACK}",
        id="stdout-then-stderr",
    ),
    pytest.param(
        "",
        "".join(f"line {i}\n" for i in range(1, 26)),
        1,
        f"{_LIST} exited 1",
        "\n".join(f"line {i}" for i in range(6, 26)),
        id="last-20-lines",
    ),
    pytest.param(
        "not json\n",
        "",
        0,
        f"{_LIST} printed no listing "
        "(JSONDecodeError('Expecting value: line 1 column 1 (char 0)'))",
        "not json",
        id="not-json",
    ),
    pytest.param(
        '{"ok": false}\n',
        "",
        0,
        f"{_LIST} printed no listing (KeyError('sidecars'))",
        '{"ok": false}',
        id="no-sidecars-key",
    ),
]
"""``sidecar list`` runs that list nothing, for :func:`stub_cli`:
``(stdout, stderr, exit code, why, shown)``. ``why`` is the reason a script
gives, and ``shown`` is the output it shows: stdout then stderr, at most
the last 20 lines."""
