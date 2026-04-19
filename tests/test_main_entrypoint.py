"""Exercise the `python -m claude_runner` module entrypoint in-process so
coverage.py can see it."""

from __future__ import annotations

import runpy

import pytest


def test_module_dunder_main_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    """`python -m claude_runner --version` exits 0.

    We run the module in-process with `run_name='__main__'` so the
    `if __name__ == "__main__"` branch is executed and counted by coverage.
    """
    monkeypatch.setattr("sys.argv", ["claude-runner", "--version"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("claude_runner", run_name="__main__")
    # --version in argparse exits with code 0.
    assert exc.value.code == 0
