"""README's copy of the CI pipeline must carry CI's coverage gate.

README.md ("Development") shows "the full lint / format / type / test
pipeline that CI runs". CI raised ``--cov-fail-under`` from 75 to 90 on
2026-05-16, and README's copy -- added the same day -- showed 75 until
2026-09-25, so anyone running the documented pipeline could pass locally
at a gate CI then failed them on. Nothing tied the two numbers together;
this test does.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).parent.parent.parent
README = REPO_ROOT / "README.md"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

_GATE = re.compile(r"--cov-fail-under(?:=|\s+)(\d+(?:\.\d+)?)")
"""Both spellings pytest-cov's argparse accepts: ``=90`` and `` 90``."""

_PIPELINE_BLOCK = re.compile(
    r"pipeline that CI runs[^\n]*\n\s*```[^\n]*\n(.*?)^```", re.DOTALL | re.MULTILINE
)
"""The fenced block right after README's "pipeline that CI runs" line."""


def _gates(script: str) -> list[float]:
    return [float(value) for value in _GATE.findall(script)]


def _readme_gates(text: str) -> list[float]:
    """The gate(s) in README's CI-pipeline block; raises if the block is gone."""
    block = _PIPELINE_BLOCK.search(text)
    if block is None:
        raise ValueError("README.md has no fenced block after 'pipeline that CI runs'")
    return _gates(block.group(1))


def _ci_gates(text: str) -> list[float]:
    """The gate(s) in every ``run:`` step of every job in the workflow.

    Read from the parsed YAML rather than grepped, so a gate quoted in a
    comment cannot pass for the one CI runs.
    """
    workflow = yaml.safe_load(text)
    return [
        gate
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        for gate in _gates(step.get("run", ""))
    ]


class TestGateParsers:
    """The parsers themselves -- one that finds nothing would compare nothing."""

    def test_reads_both_flag_spellings(self) -> None:
        assert _gates("pytest --cov --cov-fail-under=90") == [90.0]
        assert _gates("pytest --cov --cov-fail-under 87.5") == [87.5]

    def test_readme_reads_only_the_pipeline_block(self) -> None:
        text = (
            "```sh\npytest --cov-fail-under=10\n```\n\n"
            "The full lint / format / type / test pipeline that CI runs is:\n\n"
            '```sh\nmypy src\npytest -m "not live" --cov --cov-fail-under=75\n```\n\n'
            "```sh\npytest --cov-fail-under=20\n```\n"
        )
        assert _readme_gates(text) == [75.0]

    def test_readme_without_the_block_fails_loud(self) -> None:
        with pytest.raises(ValueError, match="pipeline that CI runs"):
            _readme_gates("## Development\n\nRun `pytest`.\n")

    def test_ci_reads_run_steps_not_comments(self) -> None:
        workflow = (
            "on: [push]\n"
            "jobs:\n"
            "  test:\n"
            "    steps:\n"
            "      - uses: actions/checkout@v4\n"
            "      - name: Test\n"
            "        # was --cov-fail-under=75 before 2026-05-16\n"
            "        run: pytest --cov --cov-fail-under=90\n"
            "  canary:\n"
            "    steps:\n"
            "      - run: |\n"
            "          pytest tests/integration -v\n"
        )
        assert _ci_gates(workflow) == [90.0]


class TestReadmeMatchesCi:
    # The real files' known answer is a count, not a value: the workflow
    # is the single source of truth for the number, and pinning it here
    # would make this test a third copy to keep in step.

    def test_ci_runs_exactly_one_gate(self) -> None:
        # None would make the comparison below vacuous; two would leave
        # README no single value to mirror.
        assert len(_ci_gates(CI_WORKFLOW.read_text())) == 1

    def test_readme_pipeline_shows_exactly_one_gate(self) -> None:
        assert len(_readme_gates(README.read_text())) == 1

    def test_readme_gate_matches_ci(self) -> None:
        (ci,) = _ci_gates(CI_WORKFLOW.read_text())
        (readme,) = _readme_gates(README.read_text())
        assert readme == ci, (
            f"README.md's CI pipeline block runs --cov-fail-under={readme:g}, but "
            f".github/workflows/ci.yml enforces --cov-fail-under={ci:g}. Change "
            "both in one commit (docs/cheatsheet.md quotes the gate too)."
        )
