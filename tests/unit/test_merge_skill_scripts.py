"""Run every runner-merge-claude-branches helper script, and pin how each fails.

The skill's scripts ship in the wheel, and Claude Code agents run them through
bash, python3 or Rscript; merge_branches.sh also runs
verify_branch_contributions.sh itself. None of them is imported by the
package, so a script that no test runs can break without anything going red.
Each gets a syntax or ``--help`` run here, plus cheap known-answer cases:

* ``merge_branches.sh --dry-run`` surveys the task branches and stops before it
  creates a worktree. Full runs skip the R steps they cannot do without an R
  package (``--skip-r-regen --skip-check``) and ``--skip-push``, and stand a
  stub in for the vignette validator, so every repair and verify step runs.
* The verifiers and repair scripts run against the loss they exist for: two
  task branches each add a model to the same Example-models line and append a
  canonical block at the same place, and the second ``-X theirs`` merge keeps
  only the second branch's side of both. The first branch's model and its
  whole ``### HT`` block vanish.
* Every input that cannot be right -- a ref that does not resolve, no worktree,
  a pattern matching nothing, a missing tool -- is an error with its own exit
  code, never a silent pass: a check that checks nothing reports success.
* ``verify_vignettes_parallel.R`` needs R, which CI does not install. It is
  parse-checked, and run against a worktree with no vignettes, only where
  Rscript is on PATH.

* Every repair and verify helper reads only the merge set (merge_set.py): a
  branch the pattern matches but the consolidation did not merge contributes
  nothing and is never reported missing, and a branch whose tip moved on after
  the merge contributes only the part that was merged.

restore_dropped_sections.py, union_merge_news.py and verify_no_base_reverts.py
have their own modules for their logic; here they get their CLI, fail-loud and
``--extra-ref`` cases.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from ._git_world import git
from ._merge_skill_repo import (
    SKILL_DIR,
    commit,
    consolidate,
    load_helper,
    new_repo,
    push_more,
    push_task_branch,
    run,
    run_helper,
    update_main,
)

MERGE_BRANCHES = SKILL_DIR / "merge_branches.sh"
VERIFY_CONTRIBUTIONS = SKILL_DIR / "verify_branch_contributions.sh"
VIGNETTES_R = SKILL_DIR / "verify_vignettes_parallel.R"

REGISTER = "inst/references/covariate-columns.md"  # merge_branches.sh's default --union-file
CONSOLIDATION = "consolidation"

BASE_REGISTER = """\
# Covariate columns

## Body size

### WT (**canonical for body weight**)

- **Example models:** `Base_2000_x.R` (base model).
"""
HT_BLOCK = "\n### HT (**canonical for height**)\n\n- **Example models:** `A_2020_a.R` (height).\n"
BMI_BLOCK = (
    "\n### BMI (**canonical for body-mass index**)\n\n- **Example models:** `B_2021_b.R` (bmi).\n"
)


def _with_models(*entries: str) -> str:
    """BASE_REGISTER with ``entries`` appended to the WT Example-models line."""
    return BASE_REGISTER.replace("(base model).", ", ".join(["(base model)", *entries]) + ".")


A_ENTRY = "`A_2020_a.R` (a note)"
B_ENTRY = "`B_2021_b.R` (b note)"
REGISTER_A = _with_models(A_ENTRY) + HT_BLOCK
REGISTER_B = _with_models(B_ENTRY) + BMI_BLOCK
# What the repair steps, in merge_branches.sh's order, make of REGISTER_B.
REPAIRED = _with_models(B_ENTRY, A_ENTRY) + BMI_BLOCK + HT_BLOCK

SECTION_REPORT = (
    "\n"
    "ERROR: (section-verifier) 1 branch(es) have new section headers missing from"
    f" {REGISTER}:\n"
    "    claude/a: ### HT\n"
)
PLACEMENT_REPORT = (
    "\n"
    "ERROR: (placement) 2 (canonical, model) pair(s) a branch recorded are NOT filed under"
    f" that canonical in {REGISTER}:\n"
    "    HT  <-  A_2020_a.R   (from claude/a)\n"
    "    WT  <-  A_2020_a.R   (from claude/a)\n"
    "\n"
    "    Usually two branches registered the same canonical from mains lacking each\n"
    "    other's copy, and -X theirs kept one entry. Union the surviving entry's\n"
    "    Source-aliases and Example-models with the dropped one's, keeping ONE block.\n"
)
VERIFIER_OK = (
    "    (placement) OK — every (canonical, model) pair and source alias a branch recorded is"
    f" still filed under that canonical in {REGISTER}\n"
    "    (verifier) OK — all per-branch *.R additions and brand-new ##/### canonical-section"
    f" headers are present in {REGISTER}\n"
)

# Stand-ins for verify_vignettes_parallel.R, which needs an R package to render.
DYING_VALIDATOR = '#!/bin/bash\necho "there is no package called callr" >&2\nexit 2\n'
PASSING_VALIDATOR = """\
#!/bin/bash
# Records one rendered vignette, as verify_vignettes_parallel.R does.
while [[ $# -gt 0 ]]; do [[ "$1" == --results ]] && results="$2"; shift; done
echo '{"ok":true,"file":"x.Rmd"}' > "$results"
echo "SUMMARY: 1 ok / 0 failed / 1 total"
"""
# The tools the scripts call, for building a PATH that lacks one of them.
TOOLS = (
    "Rscript",
    "awk",
    "bash",
    "cat",
    "date",
    "dirname",
    "env",
    "git",
    "grep",
    "head",
    "mkdir",
    "nproc",
    "python3",
    "rm",
    "sed",
    "sh",
    "sort",
    "tail",
    "tee",
    "tr",
    "wc",
)


def _survey_rows(stdout: str) -> list[tuple[str, str, str, str]]:
    """(branch, ahead, files, subject) for each branch merge_branches.sh surveyed."""
    return re.findall(r"^ {6}(\S+) +ahead=(\d+)  files=(\d+)  (.*)$", stdout, re.MULTILINE)


def _build_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A clone whose origin carries main and three ``claude/*`` task branches.

    ``claude/merged`` is already folded into main, so a survey must leave it
    out. ``claude/a`` and ``claude/b`` fork from that main, so each is one
    commit ahead and changes exactly the two files it pushed.
    """
    repo = new_repo(tmp_path, monkeypatch, {REGISTER: BASE_REGISTER})
    git(repo, "checkout", "-q", "-b", "claude/merged")
    commit(repo, {"inst/modeldb/M_2019_m.R": "# M\n"}, "Add M 2019 model")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "--no-edit", "claude/merged")
    git(repo, "push", "-q", "origin", "main", "claude/merged")
    push_task_branch(
        repo,
        "claude/a",
        {REGISTER: REGISTER_A, "inst/modeldb/A_2020_a.R": "# A\n"},
        "Add A 2020 model",
    )
    push_task_branch(
        repo,
        "claude/b",
        {REGISTER: REGISTER_B, "inst/modeldb/B_2021_b.R": "# B\n"},
        "Add B 2021 model",
    )
    return repo


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return _build_repo(tmp_path, monkeypatch)


@pytest.fixture
def worktree(repo: Path) -> Path:
    """The consolidation worktree after folding in claude/a, then claude/b."""
    wt = consolidate(repo, ["origin/claude/a", "origin/claude/b"], CONSOLIDATION)
    assert (wt / REGISTER).read_text() == REGISTER_B, "the fixture did not reproduce the loss"
    return wt


@pytest.fixture(scope="module")
def shared_repo(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """One consolidated repo for the tests that only read it.

    ``parked`` is a branch with no worktree, and nothing is checked out of it.
    ``nothing`` has a worktree but merged no branch at all.
    """
    with pytest.MonkeyPatch.context() as mp:
        repo = _build_repo(tmp_path_factory.mktemp("shared"), mp)
        consolidate(repo, ["origin/claude/a", "origin/claude/b"], CONSOLIDATION)
        consolidate(repo, [], "nothing")
        git(repo, "branch", "parked")
        yield repo


def _verifier_args(repo: Path) -> list[str | Path]:
    return ["--repo", repo, "--branch", CONSOLIDATION, "--file", REGISTER]


def _bin_dir(tmp_path: Path, name: str, scripts: dict[str, str]) -> Path:
    bin_dir = tmp_path / name
    bin_dir.mkdir()
    for tool, text in scripts.items():
        (bin_dir / tool).write_text(text)
        (bin_dir / tool).chmod(0o755)
    return bin_dir


def _env_with_first(bin_dir: Path) -> dict[str, str]:
    """The current environment with ``bin_dir`` first on PATH."""
    return {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}


def _env_without(tmp_path: Path, missing: str) -> dict[str, str]:
    """An environment whose PATH has every tool in TOOLS but ``missing``."""
    bin_dir = tmp_path / f"bin-without-{missing}"
    bin_dir.mkdir()
    for tool in TOOLS:
        found = shutil.which(tool)
        if tool != missing and found is not None:
            (bin_dir / tool).symlink_to(found)
    return {**os.environ, "PATH": str(bin_dir)}


def _parsed_flags(script: Path) -> set[str]:
    """Every flag the script's argument loop accepts, read from its case arms."""
    arms = re.findall(r"^\s+((?:-{1,2}[a-z][a-z-]*\|?)+)\)", script.read_text(), re.MULTILINE)
    return {flag for arm in arms for flag in arm.split("|") if flag}


@pytest.mark.parametrize("script", [MERGE_BRANCHES, VERIFY_CONTRIBUTIONS], ids=lambda p: p.name)
def test_help_documents_every_flag_the_script_parses(script: Path) -> None:
    """merge_branches.sh's help once stopped at a hard-coded line 60.

    It never showed --dry-run, --yes or the exit codes, and --register-file was
    never in it. verify_branch_contributions.sh's help ran one line too far and
    printed ``set -euo pipefail``.
    """
    proc = run("bash", script, "--help")
    assert (proc.returncode, proc.stderr) == (0, "")
    flags = _parsed_flags(script)
    assert "--help" in flags, "the argument loop was not found"
    undocumented = [
        flag
        for flag in sorted(flags)
        if not re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", proc.stdout)
    ]
    assert undocumented == []
    assert not re.search(r"^set ", proc.stdout, re.MULTILINE)


class TestMergeBranches:
    def test_parses(self) -> None:
        proc = run("bash", "-n", MERGE_BRANCHES)
        assert (proc.returncode, proc.stderr) == (0, "")

    def test_help_prints_the_whole_header(self) -> None:
        proc = run("bash", MERGE_BRANCHES, "--help")
        assert (proc.returncode, proc.stderr) == (0, "")
        assert proc.stdout.startswith(
            "Consolidate per-task claude/* branches into one review-ready branch.\n"
        )
        assert "\nUsage:\n  merge_branches.sh [OPTIONS]\n" in proc.stdout
        assert proc.stdout.endswith(
            "  9  a hand union is needed: a repair set wording aside and every step but\n"
            "     the push ran; the message names the worktree, the report of the\n"
            "     wording the register lacks, and the --finish command to run once the\n"
            "     hand union is committed. --finish without --hand-union-done also\n"
            "     exits 9 while that report holds anything.\n"
        )

    def test_unknown_argument_exits_2(self) -> None:
        proc = run("bash", MERGE_BRANCHES, "--no-such-flag")
        assert (proc.returncode, proc.stderr) == (2, "unknown arg: --no-such-flag\n")

    def test_flag_without_a_value_exits_2(self) -> None:
        proc = run("bash", MERGE_BRANCHES, "--repo")
        assert (proc.returncode, proc.stderr) == (2, "ERROR: --repo needs a value\n")

    def test_non_numeric_vignette_jobs_exits_2(self) -> None:
        proc = run("bash", MERGE_BRANCHES, "--vignette-jobs", "abc")
        assert (proc.returncode, proc.stderr) == (
            2,
            "ERROR: --vignette-jobs must be a positive integer, not 'abc'\n",
        )

    def test_dry_run_surveys_the_unmerged_branches_and_stops(self, repo: Path) -> None:
        proc = run("bash", MERGE_BRANCHES, "--repo", repo, "--dry-run")
        assert (proc.returncode, proc.stderr) == (0, ""), proc.stdout
        assert "    found 2 unmerged branch(es):\n" in proc.stdout
        assert _survey_rows(proc.stdout) == [
            ("claude/a", "1", "2", "Add A 2020 model"),
            ("claude/b", "1", "2", "Add B 2021 model"),
        ]
        assert proc.stdout.endswith("\n==> Dry-run; stopping before worktree creation.\n")
        # Nothing was created: no worktree, no consolidation branch.
        assert not (repo / ".worktrees").exists()
        listing = git(repo, "worktree", "list", "--porcelain").splitlines()
        assert len([line for line in listing if line.startswith("worktree ")]) == 1
        branches = git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()
        assert set(branches) == {"main", "claude/merged", "claude/a", "claude/b"}

    def test_survey_counts_only_the_files_a_branch_changed(self, repo: Path) -> None:
        """Main moving on after the fork must not add main's files to the count."""
        commit(repo, {"inst/other.txt": "later\n"}, "main moves on")
        git(repo, "push", "-q", "origin", "main")
        proc = run("bash", MERGE_BRANCHES, "--repo", repo, "--dry-run")
        assert proc.returncode == 0, proc.stderr
        assert [row[:3] for row in _survey_rows(proc.stdout)] == [
            ("claude/a", "1", "2"),
            ("claude/b", "1", "2"),
        ]

    def test_dry_run_aborts_when_two_branches_add_one_path_differently(self, repo: Path) -> None:
        """-X theirs would keep only the last one merged, so the survey stops.

        claude/c and claude/e add the same bytes, which is not a collision on
        its own; claude/d adds different ones. Excluding claude/d, the remedy
        the error names, lets the survey through.
        """
        model = "inst/modeldb/Wang_2019_tacrolimus.R"
        added = {"c": "# paper one\n", "d": "# paper two\n", "e": "# paper one\n"}
        for branch, text in added.items():
            push_task_branch(repo, f"claude/{branch}", {model: text}, f"Add Wang 2019 ({branch})")

        proc = run("bash", MERGE_BRANCHES, "--repo", repo, "--dry-run")
        assert proc.returncode == 4, proc.stdout
        assert proc.stderr == (
            "ERROR: the same new path is added with different content by more than one branch:\n"
            f"    {model}  <-  origin/claude/c origin/claude/d origin/claude/e\n"
            "    -X theirs would keep only the last one merged. Reletter one branch's files or"
            " --exclude-ref one of them, then re-run.\n"
        )

        proc = run(
            "bash",
            MERGE_BRANCHES,
            "--repo",
            repo,
            "--dry-run",
            "--exclude-ref",
            "origin/claude/d",
            "--exclude-ref",
            "origin/claude/gone",
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert proc.stderr == (
            "WARNING: --exclude-ref 'origin/claude/gone' matched no candidate branch\n"
        )
        assert "    excluding origin/claude/d (--exclude-ref)\n" in proc.stdout
        assert [row[0] for row in _survey_rows(proc.stdout)] == [
            "claude/a",
            "claude/b",
            "claude/c",
            "claude/e",
        ]
        assert proc.stdout.endswith("\n==> Dry-run; stopping before worktree creation.\n")

    def test_unresolvable_base_is_a_preflight_error(self, repo: Path) -> None:
        """It used to make every branch 0 ahead and report nothing to do."""
        proc = run("bash", MERGE_BRANCHES, "--repo", repo, "--base", "origin/nope", "--dry-run")
        assert (proc.returncode, proc.stderr) == (
            3,
            "ERROR: --base 'origin/nope' does not resolve to a commit\n",
        )

    def test_a_branch_without_a_merge_base_stops_the_survey(self, repo: Path) -> None:
        """Its collision check used to fail silently, so it was never checked."""
        git(repo, "checkout", "-q", "--orphan", "claude/orphan")
        git(repo, "rm", "-rq", "--cached", ".")
        commit(repo, {"inst/modeldb/A_2020_a.R": "# another paper\n"}, "Add A 2020 again")
        git(repo, "push", "-q", "origin", "claude/orphan")
        git(repo, "checkout", "-q", "-f", "main")
        proc = run("bash", MERGE_BRANCHES, "--repo", repo, "--dry-run")
        assert proc.returncode == 3, proc.stdout
        assert proc.stderr.endswith(
            "ERROR: cannot diff origin/claude/orphan against its merge base with origin/main"
            " (no merge base, or a shallow clone?)\n"
        )

    def test_excluding_every_branch_is_an_error(self, repo: Path) -> None:
        """It used to leave one empty entry, surveyed as the branch ""."""
        proc = run(
            "bash",
            MERGE_BRANCHES,
            "--repo",
            repo,
            "--dry-run",
            "--exclude-ref",
            "origin/claude/a",
            "--exclude-ref",
            "origin/claude/b",
            "--exclude-ref",
            "origin/claude/merged",
        )
        assert (proc.returncode, proc.stderr) == (
            3,
            "ERROR: no branches matched refspec 'origin/claude/*' under refs/remotes/"
            " (after any --exclude-ref) and no --extra-ref supplied\n",
        )

    def test_without_a_terminal_it_stops_at_the_prompt(self, repo: Path) -> None:
        """An agent's shell: read got EOF and the run ended with exit 1, silently."""
        proc = run(
            "bash",
            MERGE_BRANCHES,
            "--repo",
            repo,
            "--skip-r-regen",
            "--skip-check",
            "--skip-vignettes",
            "--skip-push",
        )
        assert (proc.returncode, proc.stderr) == (
            3,
            "ERROR: stdin is not a terminal, so nobody can confirm merging 2 branches."
            " Re-run with --yes to proceed without the prompt.\n",
        )
        assert not (repo / ".worktrees").exists()

    @pytest.mark.parametrize(
        ("missing", "message"),
        [
            (
                "Rscript",
                "ERROR: Rscript is not on PATH. Install R, or pass --skip-r-regen,"
                " --skip-check and --skip-vignettes and run those steps elsewhere.\n",
            ),
            (
                "python3",
                "ERROR: python3 is not on PATH; the union-merge, dedup, restore and verify"
                " steps need it.\n",
            ),
        ],
    )
    def test_a_missing_tool_stops_it_before_anything_is_created(
        self, missing: str, message: str, repo: Path, tmp_path: Path
    ) -> None:
        """Rscript used to be looked for only after the merges."""
        proc = run(
            "bash", MERGE_BRANCHES, "--repo", repo, "--yes", env=_env_without(tmp_path, missing)
        )
        assert (proc.returncode, proc.stderr) == (3, message), proc.stdout
        assert not (repo / ".worktrees").exists()

    def _full_run(
        self, repo: Path, env: dict[str, str], *extra: str
    ) -> subprocess.CompletedProcess[str]:
        """Everything but the R regeneration, devtools::check and the push."""
        return run(
            "bash",
            MERGE_BRANCHES,
            "--repo",
            repo,
            "--yes",
            "--skip-r-regen",
            "--skip-check",
            "--skip-push",
            "--branch-name",
            CONSOLIDATION,
            *extra,
            env=env,
        )

    def test_full_run_repairs_the_merge_and_passes_the_gates(
        self, repo: Path, tmp_path: Path
    ) -> None:
        env = _env_with_first(_bin_dir(tmp_path, "bin", {"Rscript": PASSING_VALIDATOR}))
        proc = self._full_run(repo, env)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert (repo / ".worktrees" / CONSOLIDATION / REGISTER).read_text() == REPAIRED
        assert VERIFIER_OK in proc.stdout
        assert "SUMMARY: 1 ok / 0 failed / 1 total\n    all vignettes rendered cleanly\n" in (
            proc.stdout
        )
        assert "\nMerge 2 claude/* branches (2 new models)\n" in proc.stdout

    def test_a_validator_that_dies_before_any_result_fails_the_gate(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """It used to find no "ok":false, report every vignette clean, and push."""
        env = _env_with_first(_bin_dir(tmp_path, "bin", {"Rscript": DYING_VALIDATOR}))
        proc = self._full_run(repo, env)
        assert proc.returncode == 8, proc.stdout
        assert "all vignettes rendered cleanly" not in proc.stdout
        assert (
            "ERROR: the vignette validator exited 2 without recording a result. Its last lines:\n"
            "    there is no package called callr\n"
        ) in proc.stderr

    def test_a_verifier_that_cannot_run_stops_the_run(self, repo: Path, tmp_path: Path) -> None:
        """Exit 1 is a verdict to reconcile by hand; exit 2 is no verdict at all."""
        crashing_python = (
            f'#!/bin/bash\ncase "$1" in */verify_section_headers.py) exit 3 ;; esac\n'
            f'exec {sys.executable} "$@"\n'
        )
        env = _env_with_first(_bin_dir(tmp_path, "bin", {"python3": crashing_python}))
        proc = self._full_run(repo, env, "--skip-vignettes")
        assert proc.returncode == 5, proc.stdout
        assert proc.stderr.endswith(
            "ERROR: (verifier) verify_section_headers.py could not run (exit 3)\n"
            f"ERROR: verify_branch_contributions.sh could not run on {REGISTER} (exit 2).\n"
        )

    def test_losses_the_repairs_cannot_fix_are_a_warning(self, repo: Path, tmp_path: Path) -> None:
        """Two branches register one canonical under different header text.

        -X theirs keeps claude/b2's block. The union-merger buckets by the whole
        header, so it cannot fold claude/a2's model back in; that is left to the
        operator, and the run finishes.
        """
        for name, model in (("a2", "A2_2020_x.R"), ("b2", "B2_2021_y.R")):
            text = BASE_REGISTER + (
                f"\n### LKST (**canonical for lkst, from {name}**)\n\n"
                f"- **Example models:** `{model}` ({name}).\n"
            )
            push_task_branch(repo, f"claude/{name}", {REGISTER: text}, f"Register LKST ({name})")
        proc = self._full_run(
            repo,
            _env_with_first(_bin_dir(tmp_path, "bin", {})),
            "--skip-vignettes",
            "--exclude-ref",
            "origin/claude/a",
            "--exclude-ref",
            "origin/claude/b",
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert f"WARNING: verifier reported missing contributions in {REGISTER}.\n" in proc.stdout
        assert "    LKST  <-  A2_2020_x.R   (from claude/a2)\n" in proc.stdout

    def test_a_branch_left_out_leaves_no_trace_in_the_register(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """Every repair and verify step used to read it anyway.

        On 2026-09-29 the 18 branches left out with --exclude-ref put 8 orphan
        Example-models entries into covariate-columns.md, and the verifiers
        reported their contributions missing.
        """
        _branch_left_out(repo)
        proc = self._full_run(
            repo,
            _env_with_first(_bin_dir(tmp_path, "bin", {})),
            "--skip-vignettes",
            "--exclude-ref",
            "origin/claude/c",
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert (repo / ".worktrees" / CONSOLIDATION / REGISTER).read_text() == REPAIRED
        assert VERIFIER_OK in proc.stdout
        assert (
            f"    (reverts) OK — nothing origin/main has in {REGISTER} was reverted by the merge\n"
        ) in proc.stdout

    def test_register_content_the_merge_reverted_stops_the_run(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """Main rewords the WT line and adds a note under it after both branches forked.

        Both branches rewrote that line, so -X theirs keeps their older copy,
        without the note. The union merger puts main's longer annotation back,
        since it rebuilds Example-models lines, but nothing restores the note:
        the run stops before the R steps, and repairs nothing.
        """
        reworded = "(base model, reworded on main).\n- **Notes:** Added on main."
        update_main(repo, {REGISTER: BASE_REGISTER.replace("(base model).", reworded)}, "note")
        proc = self._full_run(
            repo, _env_with_first(_bin_dir(tmp_path, "bin", {})), "--skip-vignettes"
        )
        wt = repo / ".worktrees" / CONSOLIDATION
        assert proc.returncode == 5, proc.stdout
        assert (
            f"\nERROR: (reverts) 1 block(s) of {REGISTER} lost content that origin/main has:\n"
            "    ## Body size\n"
            "    ### WT (**canonical for body weight**)\n"
            "      missing line: - **Notes:** Added on main.\n"
        ) in proc.stdout
        assert proc.stderr.endswith(
            "ERROR: the merge reverted register content origin/main has (listed above). Nothing"
            f" was repaired: restore it by hand in {wt}, commit, and re-run"
            " verify_no_base_reverts.py until it passes before regenerating and pushing.\n"
        )
        assert "==> Regenerating" not in proc.stdout
        assert (wt / REGISTER).read_text() == (
            BASE_REGISTER.replace(
                "(base model).",
                "(base model, reworded on main), `B_2021_b.R` (b note), `A_2020_a.R` (a note).",
            )
            + BMI_BLOCK
            + HT_BLOCK
        )


AGE_SECTION = (
    "\n## Age\n\n### AGE (**canonical for age**)\n\n- **Example models:** `Base_2000_y.R` (age).\n"
)
FOO_A = "\n### FOO (**canonical for foo**)\n\n- **Notes:** Alpha note.\n- **Example models:** `Fa_2020_a.R` (a).\n"
FOO_B = (
    "\n### FOO (**canonical for foo**)\n\n- **Notes:** Beta note, the longer one.\n"
    "- **Example models:** `Fb_2021_b.R` (b).\n"
)


class TestMergeBranchesHandUnion:
    """claude/fa and claude/fb each register FOO, worded differently and in
    different places, so -X theirs keeps both copies and the dedup collapses
    them into claude/fb's, the longer."""

    @pytest.fixture
    def hand_repo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        repo = new_repo(tmp_path, monkeypatch, {REGISTER: BASE_REGISTER + AGE_SECTION})
        fa = BASE_REGISTER + FOO_A + AGE_SECTION
        push_task_branch(repo, "claude/fa", {REGISTER: fa}, "Add Fa 2020 model")
        fb = BASE_REGISTER + AGE_SECTION + FOO_B
        push_task_branch(repo, "claude/fb", {REGISTER: fb}, "Add Fb 2021 model")
        return repo

    @staticmethod
    def _run(
        repo: Path, *flags: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return run(
            "bash",
            MERGE_BRANCHES,
            "--repo",
            repo,
            "--branch-name",
            CONSOLIDATION,
            *flags,
            env=env,
        )

    @staticmethod
    def _report(wt: Path) -> Path:
        return Path(
            git(wt, "rev-parse", "--path-format=absolute", "--git-path", "hand-union-report.md")
        )

    @staticmethod
    def _printed_finish(stderr: str) -> list[str]:
        """The --finish command the STOP message prints, as words."""
        return shlex.split(stderr.rstrip("\n").splitlines()[-1])

    def test_a_hand_union_stops_before_the_push_and_finish_completes_it(
        self, hand_repo: Path
    ) -> None:
        """Every step but the push runs; the register is collapsed and
        committed, the dropped wording is only in the report, and the command
        the message prints pushes once the hand union is committed. --finish
        without --hand-union-done prints the report and pushes nothing."""
        proc = self._run(hand_repo, "--yes", "--skip-r-regen", "--skip-check", "--skip-vignettes")
        wt = hand_repo / ".worktrees" / CONSOLIDATION
        report = self._report(wt)
        assert proc.returncode == 9, proc.stdout + proc.stderr
        finish = (
            f"bash {shlex.quote(str(MERGE_BRANCHES))} --repo {shlex.quote(str(hand_repo))}"
            f" --base origin/main --branch-name {CONSOLIDATION} --finish"
        )
        assert proc.stderr.endswith(
            "\n"
            "STOP: every step but the push ran, but a repair set wording aside: copies of\n"
            "  a canonical, or of a model's annotation, worded differently. The register\n"
            "  keeps one wording; each other is in\n"
            f"    {report}\n"
            "  (it goes with the worktree, git worktree remove, so copy it to keep it).\n"
            f"  1. In the worktree {wt}, union that wording into the register\n"
            "     by hand, keeping each source's attribution, and commit.\n"
            "  2. Then push the branch and print the PR text:\n"
            f"     {finish} --hand-union-done\n"
        )
        assert (
            f"# HAND UNION NEEDED in {REGISTER}: ### FOO: the copies differ in Notes; collapsed,"
            f" the dropped wording is in {report}\n"
        ) in proc.stderr
        assert "NOTE: a hand union is still pending" not in proc.stderr
        assert "(placement) OK" in proc.stdout
        assert "==> Pushing" not in proc.stdout
        assert git(hand_repo, "ls-remote", "origin", CONSOLIDATION) == ""
        assert git(wt, "status", "--porcelain") == ""
        collapsed = BASE_REGISTER + AGE_SECTION + FOO_B.replace("(b).", "(b), `Fa_2020_a.R` (a).")
        assert (wt / REGISTER).read_text() == collapsed
        assert (
            "In the register:\n\n````\n- **Notes:** Beta note, the longer one.\n````\n\n"
            "In a dropped copy:\n\n````\n- **Notes:** Alpha note.\n````\n"
        ) in report.read_text()
        assert "Alpha note" not in collapsed

        unioned = collapsed.replace("the longer one.", "the longer one. Alpha note.")
        (wt / REGISTER).write_text(unioned)
        printed = self._printed_finish(proc.stderr)
        refused = run(*printed)
        assert (refused.returncode, refused.stderr) == (
            3,
            f"ERROR: --finish: {wt} has uncommitted changes; commit the hand union first.\n",
        )
        git(wt, "commit", "-qam", "Union the FOO notes by hand")

        unconfirmed = run(*shlex.split(finish))
        assert unconfirmed.returncode == 9, unconfirmed.stdout + unconfirmed.stderr
        assert unconfirmed.stderr == (
            report.read_text() + "\nSTOP: the hand-union report above is not empty:\n"
            f"    {report}\n"
            f"  Union its wording into the register in {wt} and commit, then run\n"
            f"     {finish} --hand-union-done\n"
        )
        assert git(hand_repo, "ls-remote", "origin", CONSOLIDATION) == ""

        done = run(*printed)
        assert done.returncode == 0, done.stdout + done.stderr
        assert f"==> Finishing {CONSOLIDATION} in {wt}\n" in done.stdout
        assert f"    hand union confirmed (--hand-union-done); the report stays at {report}\n" in (
            done.stdout
        )
        assert f"==> Pushing {CONSOLIDATION} to origin\n" in done.stdout
        assert "\nMerge 2 claude/* branches (2 new models)\n" in done.stdout
        pushed = git(hand_repo, "ls-remote", "origin", f"refs/heads/{CONSOLIDATION}").split()[0]
        assert pushed == git(wt, "rev-parse", "HEAD")
        assert git(wt, "show", f"{pushed}:{REGISTER}") + "\n" == unioned

    def test_a_later_failure_names_the_pending_report(
        self, hand_repo: Path, tmp_path: Path
    ) -> None:
        """A gate that fails after the dedup names the pending report and points
        at --finish, which will not push past it, rather than at a manual push."""
        env = _env_with_first(_bin_dir(tmp_path, "bin", {"Rscript": DYING_VALIDATOR}))
        proc = self._run(hand_repo, "--yes", "--skip-r-regen", "--skip-check", env=env)
        wt = hand_repo / ".worktrees" / CONSOLIDATION
        assert proc.returncode == 8, proc.stdout + proc.stderr
        assert "push manually" not in proc.stderr
        assert proc.stderr.endswith(
            "When it is green, commit, then push and print the PR text with\n"
            f"  bash {shlex.quote(str(MERGE_BRANCHES))} --repo {shlex.quote(str(hand_repo))}"
            f" --base origin/main --branch-name {CONSOLIDATION} --finish\n"
            "\n"
            "NOTE: a hand union is still pending. The wording the register lacks is in\n"
            f"    {self._report(wt)}\n"
            "  Union it into the register and commit before the push; the report goes\n"
            "  with the worktree (git worktree remove), so copy it elsewhere to keep it.\n"
        )

    def test_a_rerun_over_the_worktree_names_its_report(self, hand_repo: Path) -> None:
        first = self._run(hand_repo, "--yes", "--skip-r-regen", "--skip-check", "--skip-vignettes")
        assert first.returncode == 9, first.stderr
        again = self._run(hand_repo, "--yes", "--skip-r-regen", "--skip-check", "--skip-vignettes")
        wt = hand_repo / ".worktrees" / CONSOLIDATION
        assert again.returncode == 3
        assert (
            f"ERROR: worktree already exists at {wt}\n"
            "  It holds a hand-union report, which removing it deletes; finish that run\n"
            "  with --finish, or copy the report first:\n"
            f"    {self._report(wt)}\n"
        ) in again.stderr

    def test_finish_needs_the_worktree(self, hand_repo: Path) -> None:
        proc = self._run(hand_repo, "--finish")
        assert (proc.returncode, proc.stderr) == (
            3,
            f"ERROR: --finish: no worktree at {hand_repo}/.worktrees/{CONSOLIDATION};"
            " run without --finish first.\n",
        )

    def test_finish_refuses_a_duplicate_left_in_the_register(self, hand_repo: Path) -> None:
        """A hand union that put a second copy back is caught before the push."""
        wt = consolidate(hand_repo, ["origin/claude/fa", "origin/claude/fb"], CONSOLIDATION)
        proc = self._run(hand_repo, "--finish", "--no-register-files")
        assert proc.returncode == 5, proc.stdout + proc.stderr
        assert proc.stderr.endswith(f"ERROR: duplicate canonical headers remain in {REGISTER}.\n")
        assert git(hand_repo, "ls-remote", "origin", CONSOLIDATION) == ""
        assert wt.is_dir()

    def test_finish_refuses_a_register_the_base_has_and_the_worktree_lost(
        self, hand_repo: Path
    ) -> None:
        """A register the base has and the worktree lacks stops the push."""
        wt = consolidate(hand_repo, ["origin/claude/fa"], CONSOLIDATION)
        git(wt, "rm", "-q", REGISTER)
        git(wt, "commit", "-qm", "lose the register")
        proc = self._run(hand_repo, "--finish", "--no-register-files")
        assert (proc.returncode, proc.stderr.splitlines()[-1]) == (
            5,
            f"ERROR: {REGISTER} is on origin/main but missing from the worktree; restore it"
            " before the push.",
        )

    def test_a_dedup_that_crashes_stops_the_run(self, hand_repo: Path, tmp_path: Path) -> None:
        failing = (
            '#!/bin/bash\ncase "$1" in */dedup_canonical_headers.py) exit 1 ;; esac\n'
            f'exec {sys.executable} "$@"\n'
        )
        env = _env_with_first(_bin_dir(tmp_path, "bin", {"python3": failing}))
        proc = self._run(
            hand_repo, "--yes", "--skip-r-regen", "--skip-check", "--skip-vignettes", env=env
        )
        assert (proc.returncode, proc.stderr.splitlines()[-1]) == (
            5,
            f"ERROR: dedup_canonical_headers.py crashed on {REGISTER} (exit 1); see its output"
            " above.",
        )
        assert "hand union" not in proc.stderr.lower()

    def test_the_copy_main_has_is_kept_so_the_revert_check_stays_quiet(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """claude/late, cut before main registered FOO, registers it too, at more
        length. The dedup keeps main's copy and reports the branch's wording, so
        the revert check finds nothing of main's reverted and the run stops for
        the hand union (exit 9)."""
        repo = new_repo(tmp_path, monkeypatch, {REGISTER: BASE_REGISTER + AGE_SECTION})
        late = BASE_REGISTER + AGE_SECTION + FOO_B
        push_task_branch(repo, "claude/late", {REGISTER: late}, "Add Fb 2021 model")
        main = BASE_REGISTER + FOO_A + AGE_SECTION
        update_main(repo, {REGISTER: main}, "Register FOO")
        proc = self._run(repo, "--yes", "--skip-r-regen", "--skip-check", "--skip-vignettes")
        wt = repo / ".worktrees" / CONSOLIDATION
        assert proc.returncode == 9, proc.stdout + proc.stderr
        assert "(reverts) OK" in proc.stdout
        assert (wt / REGISTER).read_text() == BASE_REGISTER + FOO_A.replace(
            "(a).", "(a), `Fb_2021_b.R` (b)."
        ) + AGE_SECTION
        assert "- **Notes:** Beta note, the longer one.\n" in self._report(wt).read_text()

    def test_a_wrapped_field_the_dedup_adds_to_is_no_revert(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Main's FOO lists its models over two lines. The dedup keeps main's
        copy and adds claude/late's model to the second line; the revert check
        reads the field whole, so it finds nothing of main's gone."""
        wrapped = FOO_A.replace("(a).", "(a),\n  `Fa2_2020_a.R` (a2).")
        repo = new_repo(tmp_path, monkeypatch, {REGISTER: BASE_REGISTER + AGE_SECTION})
        late = BASE_REGISTER + AGE_SECTION + FOO_B
        push_task_branch(repo, "claude/late", {REGISTER: late}, "Add Fb 2021 model")
        update_main(repo, {REGISTER: BASE_REGISTER + wrapped + AGE_SECTION}, "Register FOO")
        proc = self._run(repo, "--yes", "--skip-r-regen", "--skip-check", "--skip-vignettes")
        wt = repo / ".worktrees" / CONSOLIDATION
        assert proc.returncode == 9, proc.stdout + proc.stderr
        assert "(reverts) OK" in proc.stdout
        assert (wt / REGISTER).read_text() == BASE_REGISTER + wrapped.replace(
            "(a2).", "(a2), `Fb_2021_b.R` (b)."
        ) + AGE_SECTION

    def test_the_union_and_the_restore_add_to_the_same_report(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """claude/long and claude/short annotate N differently on the WT line;
        claude/one and claude/two add TWICE in different words and claude/zap
        overwrites the region after them. The report holds the union's
        set-aside note and the restore's other copy."""
        twice = "\n### TWICE (**canonical for twice**)\n\n- **Notes:** {}\n"
        repo = new_repo(tmp_path, monkeypatch, {REGISTER: BASE_REGISTER + AGE_SECTION})
        for name, note in (("long", "(a longer note on N)"), ("short", "(short)")):
            text = _with_models(f"`N_2020_n.R` {note}") + AGE_SECTION
            push_task_branch(repo, f"claude/{name}", {REGISTER: text}, f"Add N ({name})")
        for name, words in (("one", "First wording."), ("two", "Second wording, longer.")):
            text = BASE_REGISTER + AGE_SECTION + twice.format(words)
            push_task_branch(repo, f"claude/{name}", {REGISTER: text}, f"Register TWICE ({name})")
        zap = BASE_REGISTER + AGE_SECTION + "\n### ZAP (**canonical for zap**)\n"
        push_task_branch(repo, "claude/zap", {REGISTER: zap}, "Register ZAP")
        proc = self._run(repo, "--yes", "--skip-r-regen", "--skip-check", "--skip-vignettes")
        wt = repo / ".worktrees" / CONSOLIDATION
        assert proc.returncode == 9, proc.stdout + proc.stderr
        report = self._report(wt).read_text()
        assert "In a merged copy:\n\n````\n`N_2020_n.R` (short)\n````\n" in report
        assert "In that copy:\n\n````\n### TWICE (**canonical for twice**)\n" in report

    def test_hand_union_done_goes_with_finish(self, hand_repo: Path) -> None:
        proc = self._run(hand_repo, "--yes", "--hand-union-done")
        assert (proc.returncode, proc.stderr) == (
            2,
            "ERROR: --hand-union-done goes with --finish\n",
        )

    def test_a_confirmed_finish_whose_push_fails_says_nothing_of_a_pending_union(
        self, hand_repo: Path, tmp_path: Path
    ) -> None:
        proc = self._run(hand_repo, "--yes", "--skip-r-regen", "--skip-check", "--skip-vignettes")
        assert proc.returncode == 9, proc.stderr
        shutil.rmtree(tmp_path / "origin.git")
        done = run(*self._printed_finish(proc.stderr))
        assert done.returncode == 7, done.stdout + done.stderr
        assert "hand union is still pending" not in done.stderr

    def test_a_run_killed_after_the_dedup_names_the_report(
        self, hand_repo: Path, tmp_path: Path
    ) -> None:
        """A TERM (or Ctrl-C) exits with its own status, so the EXIT trap names
        the pending report."""
        killer = (
            '#!/bin/bash\ncase "$1" in */restore_dropped_sections.py) kill -TERM "$PPID"; exit 0 ;;'
            f' esac\nexec {sys.executable} "$@"\n'
        )
        env = _env_with_first(_bin_dir(tmp_path, "bin", {"python3": killer}))
        proc = self._run(
            hand_repo, "--yes", "--skip-r-regen", "--skip-check", "--skip-vignettes", env=env
        )
        assert proc.returncode == 143, proc.stdout + proc.stderr
        assert "NOTE: a hand union is still pending." in proc.stderr

    @pytest.mark.parametrize(
        "registers",
        [
            ("--no-register-files",),
            ("--no-register-files", "--register-file", "refs/comp names.md"),
        ],
        ids=["none", "custom"],
    )
    def test_the_printed_finish_carries_the_runs_registers_and_the_report_accumulates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, registers: tuple[str, ...]
    ) -> None:
        """A custom union file and register: both hand unions are in one report,
        and the printed --finish, run as printed, finds both files."""
        union_file = "refs/covariates.md"
        comp = "refs/comp names.md"
        comp_base = "## Compartments\n\n### depot (**dosing compartment**)\n\n- **Role:** state.\n"
        bar = "\n### BAR (**canonical for bar**)\n\n- **Role:** {role}\n- **Example models:** `{m}` (x).\n"
        repo = new_repo(
            tmp_path, monkeypatch, {union_file: BASE_REGISTER + AGE_SECTION, comp: comp_base}
        )
        files_a = {
            union_file: BASE_REGISTER + FOO_A + AGE_SECTION,
            comp: comp_base.replace(
                "\n### depot", bar.format(role="a.", m="Fa_2020_a.R") + "\n### depot"
            ),
        }
        files_b = {
            union_file: BASE_REGISTER + AGE_SECTION + FOO_B,
            comp: comp_base + bar.format(role="b, the longer role.", m="Fb_2021_b.R"),
        }
        push_task_branch(repo, "claude/fa", files_a, "Add Fa 2020 model")
        push_task_branch(repo, "claude/fb", files_b, "Add Fb 2021 model")
        proc = self._run(
            repo,
            "--yes",
            "--skip-r-regen",
            "--skip-check",
            "--skip-vignettes",
            "--union-file",
            union_file,
            *registers,
        )
        wt = repo / ".worktrees" / CONSOLIDATION
        assert proc.returncode == 9, proc.stdout + proc.stderr
        printed = self._printed_finish(proc.stderr)
        assert printed[printed.index("--branch-name") + 2 :] == [
            "--union-file",
            union_file,
            *registers,
            "--finish",
            "--hand-union-done",
        ]
        report = self._report(wt).read_text()
        assert f"# Hand union needed in {union_file}\n" in report
        assert (f"# Hand union needed in {comp}\n" in report) == (comp in registers)
        done = run(*printed)
        assert done.returncode == 0, done.stdout + done.stderr
        assert f"# clean (no whole-file duplicate canonicals): {union_file}\n" in done.stderr
        assert (f"# clean (no per-##-section duplicate canonicals): {comp}\n" in done.stderr) == (
            comp in registers
        )
        assert git(repo, "ls-remote", "origin", f"refs/heads/{CONSOLIDATION}") != ""


class TestVerifyBranchContributions:
    """merge_branches.sh runs this one directly, so these tests do too."""

    def test_parses(self) -> None:
        proc = run("bash", "-n", VERIFY_CONTRIBUTIONS)
        assert (proc.returncode, proc.stderr) == (0, "")

    def test_help(self) -> None:
        proc = run(VERIFY_CONTRIBUTIONS, "--help")
        assert (proc.returncode, proc.stderr) == (0, "")
        assert proc.stdout.startswith(
            "Verify no per-branch model contributions were lost from a structured-\n"
        )
        assert "\nUsage:\n  verify_branch_contributions.sh [OPTIONS]\n" in proc.stdout

    def test_branch_is_required(self) -> None:
        proc = run(VERIFY_CONTRIBUTIONS)
        assert (proc.returncode, proc.stderr) == (2, "ERROR: (verifier) --branch is required\n")

    def test_unknown_argument_exits_2(self) -> None:
        proc = run(VERIFY_CONTRIBUTIONS, "--no-such-flag")
        assert (proc.returncode, proc.stderr) == (2, "unknown arg: --no-such-flag\n")

    def test_empty_file_disables_it(self) -> None:
        proc = run(VERIFY_CONTRIBUTIONS, "--branch", CONSOLIDATION, "--file", "")
        assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")

    def test_reports_the_model_and_block_the_merge_dropped(
        self, repo: Path, worktree: Path
    ) -> None:
        """All three checks fail on the post-merge file, in one report."""
        proc = run(VERIFY_CONTRIBUTIONS, *_verifier_args(repo))
        assert (proc.returncode, proc.stderr) == (1, "")
        filename_report = (
            "\n"
            f"ERROR: (verifier) 1 branch(es) have *.R contributions missing from {REGISTER}:\n"
            "    claude/a: `A_2020_a.R`\n"
        )
        footer = (
            "\n"
            f"    Worktree left at: {worktree}\n"
            "    Either re-run the union-merger, or hand-merge the missing entries.\n"
        )
        assert proc.stdout == filename_report + SECTION_REPORT + PLACEMENT_REPORT + footer

    def test_a_branch_adding_no_model_name_does_not_stop_it(
        self, repo: Path, worktree: Path
    ) -> None:
        """A grep with no match under pipefail used to end the run: exit 1, no output."""
        push_task_branch(repo, "claude/prose", {REGISTER: BASE_REGISTER + "\nA note.\n"}, "prose")
        git(repo, "fetch", "-q", "origin")
        proc = run(VERIFY_CONTRIBUTIONS, *_verifier_args(repo))
        assert (proc.returncode, proc.stderr) == (1, "")
        assert proc.stdout.startswith(
            "\n"
            f"ERROR: (verifier) 1 branch(es) have *.R contributions missing from {REGISTER}:\n"
            "    claude/a: `A_2020_a.R`\n"
        )

    def test_a_file_the_worktree_lacks_is_skipped(self, shared_repo: Path) -> None:
        proc = run(
            VERIFY_CONTRIBUTIONS, "--repo", shared_repo, "--branch", CONSOLIDATION, "--file", "x.md"
        )
        merged = shared_repo / ".worktrees" / CONSOLIDATION / "x.md"
        assert (proc.returncode, proc.stderr) == (0, "")
        assert proc.stdout == f"    (verifier) merged file not present at {merged}; skipping.\n"

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            (
                ["--base", "origin/nope"],
                "--base 'origin/nope' does not resolve to a commit in {repo}",
            ),
            (["--branch", "nope"], "--branch 'nope' does not resolve to a commit in {repo}"),
            (
                ["--branch", "parked"],
                "no worktree for --branch 'parked' at {repo}/.worktrees/parked",
            ),
            (
                ["--pattern", "origin/none/*"],
                "no branch matches --pattern 'origin/none/*' and no --extra-ref was given;"
                " nothing to verify",
            ),
            (
                ["--extra-ref", "origin/nope"],
                "--extra-ref 'origin/nope' does not resolve to a commit in {repo}",
            ),
            (["--file"], "--file needs a value"),
        ],
        ids=["base", "branch", "no-worktree", "no-match", "extra-ref", "no-value"],
    )
    def test_inputs_that_cannot_be_right_exit_2(
        self, args: list[str], message: str, shared_repo: Path
    ) -> None:
        """Each of these used to check less than it said, or nothing, and exit 0."""
        proc = run(
            VERIFY_CONTRIBUTIONS,
            "--repo",
            shared_repo,
            "--branch",
            CONSOLIDATION,
            "--file",
            REGISTER,
            *args,
        )
        assert (proc.returncode, proc.stdout) == (2, "")
        assert proc.stderr == f"ERROR: (verifier) {message.format(repo=shared_repo)}\n"

    def test_missing_python3_exits_2(self, shared_repo: Path, tmp_path: Path) -> None:
        """It used to skip the header and placement checks and report OK."""
        proc = run(
            VERIFY_CONTRIBUTIONS,
            "--repo",
            shared_repo,
            "--branch",
            CONSOLIDATION,
            "--file",
            REGISTER,
            env=_env_without(tmp_path, "python3"),
        )
        assert (proc.returncode, proc.stdout, proc.stderr) == (
            2,
            "",
            "ERROR: (verifier) python3 is not on PATH, so the header and placement checks"
            " cannot run\n",
        )


@pytest.mark.parametrize(
    "script",
    [
        "dedup_canonical_headers.py",
        "merge_set.py",
        "restore_dropped_sections.py",
        "union_merge_lines.py",
        "union_merge_news.py",
        "verify_no_base_reverts.py",
        "verify_register_placement.py",
        "verify_section_headers.py",
    ],
)
def test_python_helper_help(script: str) -> None:
    proc = run_helper(script, "--help")
    assert (proc.returncode, proc.stderr) == (0, "")
    assert proc.stdout.startswith(f"usage: {script} [-h]")


def test_section_headers_reports_the_dropped_block(repo: Path, worktree: Path) -> None:
    proc = run_helper("verify_section_headers.py", *_verifier_args(repo))
    assert (proc.returncode, proc.stdout, proc.stderr) == (1, SECTION_REPORT, "")


class TestRegisterPlacement:
    def test_reports_each_pair_the_merge_dropped(self, repo: Path, worktree: Path) -> None:
        """The WT pair is lost even though the WT block itself survived."""
        proc = run_helper("verify_register_placement.py", *_verifier_args(repo))
        assert (proc.returncode, proc.stdout, proc.stderr) == (1, PLACEMENT_REPORT, "")

    def test_unresolvable_branch_is_an_error_not_a_pass(self, shared_repo: Path) -> None:
        """Checked before anything can return early.

        With no worktree for the branch it used to report "merged file absent;
        skipping" and exit 0, never reaching this check.
        """
        proc = run_helper(
            "verify_register_placement.py",
            "--repo",
            shared_repo,
            "--branch",
            "no-such-branch",
            "--file",
            REGISTER,
        )
        assert (proc.returncode, proc.stdout) == (2, "")
        assert proc.stderr == (
            "ERROR: (placement) --branch 'no-such-branch' does not resolve to a commit in"
            f" {shared_repo}; refusing to report a vacuous pass.\n"
        )

    @pytest.mark.parametrize(
        ("block", "filed"),
        [
            ("- **Example models:** `A_2020_a.R` (refits the `B_2021_b.R` data).", {"A_2020_a.R"}),
            (
                "- **Example models:** `A_2020_a.R` and `B_2021_b.R` (y); `C_2022_c.R`.",
                {"A_2020_a.R", "B_2021_b.R", "C_2022_c.R"},
            ),
            (
                "- **Example models:** Fits of the chain: `A_2020_a.R` for the base case.",
                {"A_2020_a.R"},
            ),
            (
                "- **Example models:**\n  * `A_2020_a.R` -- star bullet.\n"
                "  - fitted in `B_2021_b.R` -- a bullet that opens with prose.",
                {"A_2020_a.R", "B_2021_b.R"},
            ),
            (
                "- **Example models:** `A_2020_a.R` (inline).\n  - `B_2021_b.R` -- under the inline list.",
                {"A_2020_a.R", "B_2021_b.R"},
            ),
            ("- **Source aliases:**\n  - `GENDER` -- used in `D_2023_d.R`.", set()),
            ("- **Notes:** Example models elsewhere cite `E_2024_e.R`.", set()),
        ],
        ids=[
            "mention-in-annotation",
            "and-semicolon",
            "prose-led",
            "sub-bullets",
            "sub-bullets-under-inline",
            "alias-mention",
            "notes-mention",
        ],
    )
    def test_a_model_is_filed_only_as_an_example_models_entry(
        self, block: str, filed: set[str]
    ) -> None:
        """A model named inside another entry's annotation, an alias bullet or
        Notes prose that says "Example models" is not filed there; one in a
        ``*`` bullet, or a bullet opening with prose, is."""
        placement = load_helper("verify_register_placement")
        text = f"## S\n\n### Q, Q2 (**canonical for q**)\n{block}\n\n### R (**r**)\n- **Type:** x\n"
        assert dict(placement.parse(text)) == ({"Q": filed, "Q2": filed} if filed else {})

    def test_a_model_left_only_in_another_entrys_annotation_is_reported(
        self, repo: Path, worktree: Path
    ) -> None:
        """The WT line lost claude/a's entry, and only claude/b's annotation still
        names its model: a mention is no filing, so the pair is reported."""
        mention = "`B_2021_b.R` (b note, refitting the `A_2020_a.R` data)"
        (worktree / REGISTER).write_text(_with_models(mention) + BMI_BLOCK + HT_BLOCK)
        proc = run_helper("verify_register_placement.py", *_verifier_args(repo))
        assert proc.returncode == 1, proc.stdout
        assert (
            "ERROR: (placement) 1 (canonical, model) pair(s) a branch recorded are NOT filed under"
            f" that canonical in {REGISTER}:\n"
            "    WT  <-  A_2020_a.R   (from claude/a)\n"
        ) in proc.stdout

    @pytest.mark.parametrize(
        ("merged_alias", "lost"),
        [
            ("", True),
            ("  - `BW`  --  used in `S_2020_s.R`\n", False),
            ("  * `BW` -- used in `S_2020_s.R`; also `BW_KG` in `T_2021_t.R`.\n", False),
        ],
        ids=["lost", "respaced", "said-more"],
    )
    def test_a_source_alias_a_branch_added_must_survive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, merged_alias: str, lost: bool
    ) -> None:
        """Each source-alias bullet a branch added is checked on its own, apart
        from any model it names. A merged bullet that holds the same words,
        respaced or inside a longer one (a hand union of two wordings), keeps
        it."""
        models = "(base model), `S_2020_s.R` (s)."
        with_alias = BASE_REGISTER.replace(
            "- **Example models:**",
            "- **Source aliases:**\n  - `BW` -- used in `S_2020_s.R`.\n- **Example models:**",
        ).replace("(base model).", models)
        repo = new_repo(tmp_path, monkeypatch, {REGISTER: BASE_REGISTER})
        push_task_branch(repo, "claude/s", {REGISTER: with_alias}, "Add S 2020 model")
        wt = consolidate(repo, ["origin/claude/s"], CONSOLIDATION)
        aliases = f"- **Source aliases:**\n{merged_alias}" if merged_alias else ""
        merged = BASE_REGISTER.replace("- **Example models:**", f"{aliases}- **Example models:**")
        (wt / REGISTER).write_text(merged.replace("(base model).", models))
        proc = run_helper("verify_register_placement.py", *_verifier_args(repo))
        if not lost:
            assert (proc.returncode, proc.stderr) == (0, ""), proc.stdout
            return
        assert proc.returncode == 1, proc.stdout
        assert proc.stdout.endswith(
            "\n"
            "ERROR: (placement) 1 source-alias bullet(s) a branch added are NOT under that"
            f" canonical in {REGISTER}:\n"
            "    WT  <-  `BW` -- used in `S_2020_s.R`.   (from claude/s)\n"
        )
        assert "(canonical, model)" not in proc.stdout

    def test_a_source_alias_is_kept_only_as_whole_words(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A merged bullet that holds the branch's bullet only inside a longer
        number ("120" in "1200") says something else, so the branch's bullet
        is reported as lost."""
        models = "(base model), `S_2020_s.R` (s)."
        bullet = "  - `CRCL` -- `S_2020_s.R`, capped at 120.\n"
        with_alias = BASE_REGISTER.replace(
            "- **Example models:**", f"- **Source aliases:**\n{bullet}- **Example models:**"
        ).replace("(base model).", models)
        repo = new_repo(tmp_path, monkeypatch, {REGISTER: BASE_REGISTER})
        push_task_branch(repo, "claude/s", {REGISTER: with_alias}, "Add S 2020 model")
        wt = consolidate(repo, ["origin/claude/s"], CONSOLIDATION)
        (wt / REGISTER).write_text(with_alias.replace("at 120.", "at 1200."))
        proc = run_helper("verify_register_placement.py", *_verifier_args(repo))
        assert proc.returncode == 1, proc.stdout
        assert proc.stdout.endswith(
            "\n"
            "ERROR: (placement) 1 source-alias bullet(s) a branch added are NOT under that"
            f" canonical in {REGISTER}:\n"
            "    WT  <-  `CRCL` -- `S_2020_s.R`, capped at 120.   (from claude/s)\n"
        )


# The helpers that read the consolidation worktree, and the prefix of their errors.
WORKTREE_HELPERS = {
    "verify_section_headers.py": "section-verifier",
    "verify_register_placement.py": "placement",
    "union_merge_lines.py": "union-merge",
    "restore_dropped_sections.py": "restore",
    "union_merge_news.py": "news",
    "verify_no_base_reverts.py": "reverts",
}
# These find the worktree with `git worktree list`; the others at
# <repo>/.worktrees/<branch>.
_LISTED = {"restore_dropped_sections.py", "union_merge_news.py", "verify_no_base_reverts.py"}
_VERIFIERS = {
    "verify_section_headers.py",
    "verify_register_placement.py",
    "verify_no_base_reverts.py",
}


def _bad_input_cases() -> Iterator[object]:
    for script, prefix in WORKTREE_HELPERS.items():
        no_match = "no branch matches --pattern 'origin/none/*' and no --extra-ref was given"
        if script in _VERIFIERS:
            no_match += "; nothing to verify"
        no_worktree = (
            "no worktree of {repo} has --branch 'parked' checked out"
            if script in _LISTED
            else "no worktree for --branch 'parked' at {repo}/.worktrees/parked"
        )
        branch = "--branch 'nope' does not resolve to a commit in {repo}"
        if script == "verify_register_placement.py":
            branch += "; refusing to report a vacuous pass."
        cases = {
            "branch": (["--branch", "nope"], branch),
            "no-worktree": (["--branch", "parked"], no_worktree),
            "no-match": (["--pattern", "origin/none/*"], no_match),
            "extra-ref": (
                ["--extra-ref", "origin/nope"],
                "--extra-ref 'origin/nope' does not resolve to a commit in {repo}",
            ),
            "base": (
                ["--base", "origin/nope"],
                "--base 'origin/nope' does not resolve to a commit in {repo}",
            ),
        }
        for case, (args, message) in cases.items():
            yield pytest.param(
                script, args, f"ERROR: ({prefix}) {message}\n", id=f"{script}-{case}"
            )


@pytest.mark.parametrize(("script", "args", "stderr"), list(_bad_input_cases()))
def test_worktree_helpers_refuse_inputs_that_cannot_be_right(
    script: str, args: list[str], stderr: str, shared_repo: Path
) -> None:
    """A wrong branch, base or pattern is exit 2, not a silent skip or pass.

    restore_dropped_sections.py and union_merge_news.py used to fall back to
    the main checkout when no worktree had --branch checked out, and would read
    and rewrite its file; restore also replaced a --pattern matching nothing
    with origin/claude/*.
    """
    main_copy = (shared_repo / REGISTER).read_text()
    proc = run_helper(
        script, "--repo", shared_repo, "--branch", CONSOLIDATION, "--file", REGISTER, *args
    )
    assert (proc.returncode, proc.stdout) == (2, "")
    assert proc.stderr == stderr.format(repo=shared_repo)
    assert (shared_repo / REGISTER).read_text() == main_copy


@pytest.mark.parametrize(
    ("script", "stdout", "stderr"),
    [
        (
            "verify_section_headers.py",
            "",
            "# (section-verifier) merged file not present at {f}; skipping.\n",
        ),
        (
            "verify_register_placement.py",
            "    (placement) merged file absent at {f}; skipping.\n",
            "",
        ),
        ("union_merge_lines.py", "", "# target file not present on branch: {f}\n"),
        (
            "restore_dropped_sections.py",
            f"x.md not present on {CONSOLIDATION}; nothing to do\n",
            "",
        ),
        ("union_merge_news.py", "x.md not present; nothing to do\n", ""),
        (
            "verify_no_base_reverts.py",
            "    (reverts) x.md is not on origin/main; nothing to check.\n",
            "",
        ),
    ],
)
def test_worktree_helpers_skip_a_file_the_worktree_lacks(
    script: str, stdout: str, stderr: str, shared_repo: Path
) -> None:
    """A repo without that register is legitimate: nothing to do, exit 0."""
    merged = shared_repo / ".worktrees" / CONSOLIDATION / "x.md"
    proc = run_helper(script, "--repo", shared_repo, "--branch", CONSOLIDATION, "--file", "x.md")
    assert (proc.returncode, proc.stdout, proc.stderr) == (
        0,
        stdout.format(f=merged),
        stderr.format(f=merged),
    )
    assert not merged.exists()


def test_union_merge_lines_restores_the_dropped_example_model(repo: Path, worktree: Path) -> None:
    """Merged order first, then each branch's new models, keeping each annotation."""
    merged = worktree / REGISTER
    proc = run_helper("union_merge_lines.py", *_verifier_args(repo))
    assert (proc.returncode, proc.stdout) == (0, "")
    assert proc.stderr == (
        f"# branches touching {REGISTER}: 2\n"
        "#   - origin/claude/a\n"
        "#   - origin/claude/b\n"
        "# (cov, sub) buckets with entries: 3\n"
        "# total filename entries:           5\n"
        f"# wrote merged file: {merged}\n"
    )
    assert merged.read_text() == _with_models(B_ENTRY, A_ENTRY) + BMI_BLOCK


def test_union_then_restore_closes_everything_the_verifier_reported(
    repo: Path, worktree: Path
) -> None:
    """merge_branches.sh's repair steps, in its order, leave the verifier clean."""
    assert run_helper("union_merge_lines.py", *_verifier_args(repo)).returncode == 0
    proc = run_helper("restore_dropped_sections.py", *_verifier_args(repo))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (worktree / REGISTER).read_text() == REPAIRED

    proc = run(VERIFY_CONTRIBUTIONS, *_verifier_args(repo))
    assert (proc.returncode, proc.stderr) == (0, "")
    assert proc.stdout == VERIFIER_OK


def test_restore_takes_extra_refs(repo: Path, worktree: Path) -> None:
    """A hand-picked branch outside --pattern gets its dropped block back too.

    --extra-ref reached only the union-merger and the verifier before, so the
    restore never saw such a branch.
    """
    args = [*_verifier_args(repo), "--pattern", "origin/claude/b"]
    proc = run_helper("restore_dropped_sections.py", *args)
    assert (proc.returncode, proc.stdout) == (0, f"# no dropped canonicals in {REGISTER}\n")
    assert (worktree / REGISTER).read_text() == REGISTER_B

    proc = run_helper("restore_dropped_sections.py", *args, "--extra-ref", "origin/claude/a")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (worktree / REGISTER).read_text() == REGISTER_B + HT_BLOCK


def test_news_union_restores_the_dropped_bullet_and_takes_extra_refs(repo: Path) -> None:
    """union_merge_news.py's first end-to-end run in a test."""
    commit(repo, {"NEWS.md": "# development version\n\n- Add Base 2000 x model.\n"}, "news")
    git(repo, "push", "-q", "origin", "main")
    for name, year in (("c", "2022"), ("d", "2023")):
        news = f"# development version\n\n- Add {name.upper()} {year} {name} model.\n\n"
        push_task_branch(
            repo,
            f"claude/{name}",
            {
                "NEWS.md": news + "- Add Base 2000 x model.\n",
                f"inst/modeldb/{name.upper()}_{year}_{name}.R": "# model\n",
            },
            f"Add {name}",
        )
    wt = consolidate(repo, ["origin/claude/c", "origin/claude/d"], "news")
    merged = wt / "NEWS.md"
    assert "Add C 2022" not in merged.read_text(), "the fixture did not reproduce the loss"
    args = ["--repo", repo, "--branch", "news", "--pattern", "origin/claude/d"]

    proc = run_helper("union_merge_news.py", *args)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.endswith("# NEWS.md already complete; nothing to do\n")

    proc = run_helper("union_merge_news.py", *args, "--extra-ref", "origin/claude/c")
    assert proc.returncode == 0, proc.stderr
    assert merged.read_text() == (
        "# development version\n\n"
        "- Add D 2023 d model.\n\n"
        "- Add C 2022 c model.\n\n"
        "- Add Base 2000 x model.\n"
    )


def _empty_merge_set_message(prefix: str, suffix: str = "") -> str:
    return (
        f"ERROR: ({prefix}) no branch matching --pattern 'origin/claude/*' or given with"
        " --extra-ref is in the merge set of nothing: it merged none of them, or origin/main"
        f" already has them{suffix}\n"
    )


@pytest.mark.parametrize("script", sorted(WORKTREE_HELPERS))
def test_worktree_helpers_refuse_an_empty_merge_set(script: str, shared_repo: Path) -> None:
    """A consolidation that merged none of the refs leaves nothing to repair or check.

    Passing would be the vacuous pass a verifier must never give: the branch
    or the pattern is wrong.
    """
    main_copy = (shared_repo / REGISTER).read_text()
    proc = run_helper(script, "--repo", shared_repo, "--branch", "nothing", "--file", REGISTER)
    suffix = "; nothing to verify" if script in _VERIFIERS else ""
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert proc.stderr.endswith(_empty_merge_set_message(WORKTREE_HELPERS[script], suffix))
    # The gate says why: claude/a and claude/b were not merged into it.
    gate = "merge-set gate: skipped 2 branch(es) that nothing did not merge"
    assert (proc.stdout + proc.stderr).count(gate) == 1
    assert (shared_repo / REGISTER).read_text() == main_copy


def test_verify_contributions_refuses_an_empty_merge_set(shared_repo: Path) -> None:
    proc = run(
        VERIFY_CONTRIBUTIONS, "--repo", shared_repo, "--branch", "nothing", "--file", REGISTER
    )
    assert (proc.returncode, proc.stdout) == (2, "")
    assert proc.stderr == _empty_merge_set_message("merge-set") + (
        "ERROR: (verifier) merge_set.py could not compute the merge set of 'nothing'\n"
    )


def _sha(repo: Path, ref: str) -> str:
    return git(repo, "rev-parse", ref)


class TestMergeSet:
    """merge_set.py decides, for every helper, which refs a consolidation merged."""

    def test_prints_each_member_with_its_merged_commit_and_fork_point(
        self, shared_repo: Path
    ) -> None:
        """claude/merged is already on the base: not a member, and not worth a line."""
        proc = run_helper("merge_set.py", "--repo", shared_repo, "--branch", CONSOLIDATION)
        fork = _sha(shared_repo, "origin/main")
        a, b = _sha(shared_repo, "origin/claude/a"), _sha(shared_repo, "origin/claude/b")
        assert (proc.returncode, proc.stderr) == (0, "")
        assert proc.stdout == f"origin/claude/a {a} {fork}\norigin/claude/b {b} {fork}\n"

    def test_a_branch_left_out_is_skipped_and_one_that_moved_on_is_read_where_merged(
        self, repo: Path
    ) -> None:
        consolidate(repo, ["origin/claude/a"], CONSOLIDATION)
        merged = _sha(repo, "origin/claude/a")
        push_more(repo, "claude/a", {"inst/modeldb/A_2020_z.R": "# later\n"}, "Add A 2020 z")
        tip = _sha(repo, "origin/claude/a")
        args = ["--repo", repo, "--branch", CONSOLIDATION]
        proc = run_helper("merge_set.py", *args)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == f"origin/claude/a {merged} {_sha(repo, 'origin/main')}\n"
        assert proc.stderr == (
            "# merge-set gate: skipped 1 branch(es) that consolidation did not merge (left out"
            " with --exclude-ref, or pushed after the survey)\n"
            "# merge-set gate: origin/claude/a moved on after consolidation merged it; reading"
            f" the merged commit {merged[:12]}, not its tip {tip[:12]}\n"
        )
        quiet = run_helper("merge_set.py", *args, "--quiet")
        assert (quiet.returncode, quiet.stdout, quiet.stderr) == (0, proc.stdout, "")

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            (
                ["--base", "origin/nope"],
                "--base 'origin/nope' does not resolve to a commit in {repo}",
            ),
            (["--branch", "nope"], "--branch 'nope' does not resolve to a commit in {repo}"),
            (
                ["--pattern", "origin/none/*"],
                "no branch matches --pattern 'origin/none/*' and no --extra-ref was given",
            ),
        ],
        ids=["base", "branch", "no-match"],
    )
    def test_inputs_that_cannot_be_right_exit_2(
        self, args: list[str], message: str, shared_repo: Path
    ) -> None:
        proc = run_helper("merge_set.py", "--repo", shared_repo, "--branch", CONSOLIDATION, *args)
        assert (proc.returncode, proc.stdout) == (2, "")
        assert proc.stderr == f"ERROR: (merge-set) {message.format(repo=shared_repo)}\n"


C_ENTRY = "`C_2022_c.R` (c note)"
AGE_BLOCK = "\n### AGE (**canonical for age**)\n\n- **Example models:** `C_2022_c.R` (age).\n"


def _branch_left_out(repo: Path) -> None:
    """claude/c adds a model and a canonical of its own; the consolidation leaves it out."""
    push_task_branch(
        repo,
        "claude/c",
        {REGISTER: _with_models(C_ENTRY) + AGE_BLOCK, "inst/modeldb/C_2022_c.R": "# C\n"},
        "Add C 2022 model",
    )


LATER_BLOCK = "\n### LATER (**canonical pushed after the merge**)\n\n- **Example models:** `A_2020_z.R` (z).\n"


def _branch_moves_on(repo: Path) -> None:
    """claude/a, already merged, gets another model and canonical it was not merged with."""
    later = _with_models(A_ENTRY, "`A_2020_z.R` (z note)") + HT_BLOCK + LATER_BLOCK
    push_more(repo, "claude/a", {REGISTER: later}, "Add A 2020 z")


class TestMergeSetGate:
    """R1 of the 2026-09-29 fixes: every helper reads only what was merged.

    On that round the 18 branches left out with --exclude-ref put 8 orphan
    Example-models entries into covariate-columns.md, and the verifiers
    reported dozens of their contributions as missing.
    """

    def test_union_ignores_a_branch_left_out_and_what_a_branch_pushed_later(
        self, repo: Path, worktree: Path
    ) -> None:
        _branch_left_out(repo)
        _branch_moves_on(repo)
        git(repo, "fetch", "-q", "origin")
        proc = run_helper("union_merge_lines.py", *_verifier_args(repo))
        assert (proc.returncode, proc.stdout) == (0, ""), proc.stderr
        assert (worktree / REGISTER).read_text() == _with_models(B_ENTRY, A_ENTRY) + BMI_BLOCK
        assert proc.stderr.startswith(
            "# merge-set gate: skipped 1 branch(es) that consolidation did not merge (left out"
            " with --exclude-ref, or pushed after the survey)\n"
            "# merge-set gate: origin/claude/a moved on after consolidation merged it;"
        )

    def test_verifiers_do_not_report_what_was_never_merged(
        self, repo: Path, worktree: Path
    ) -> None:
        """Both would fail every check: claude/c's model and AGE, and claude/a's LATER."""
        _branch_left_out(repo)
        _branch_moves_on(repo)
        git(repo, "fetch", "-q", "origin")
        assert run_helper("union_merge_lines.py", *_verifier_args(repo)).returncode == 0
        assert run_helper("restore_dropped_sections.py", *_verifier_args(repo)).returncode == 0
        assert (worktree / REGISTER).read_text() == REPAIRED

        proc = run(VERIFY_CONTRIBUTIONS, *_verifier_args(repo))
        assert (proc.returncode, proc.stderr) == (0, ""), proc.stdout
        a_merged = _sha(repo, "consolidation~1^2")  # claude/a was merged first, then claude/b
        gate = (
            "    (placement) merge-set gate: skipped 1 branch(es) that consolidation did not"
            " merge (left out with --exclude-ref, or pushed after the survey)\n"
            "    (placement) merge-set gate: origin/claude/a moved on after consolidation merged"
            f" it; reading the merged commit {a_merged[:12]}, not its tip"
            f" {_sha(repo, 'origin/claude/a')[:12]}\n"
        )
        assert proc.stdout == gate + VERIFIER_OK


NAGY = "`Nagy_2017_x.R` (quartiles -- [BLQ, 3.02], (4.87, 8.56] log10 CFU)"
TRICKY_REGISTER = f"""\
# Covariate columns

## Body size

### WT (**canonical for body weight**)

- **Example models:** {NAGY}.............
- **Notes:** A "(" that never closes, as three real annotations have.

### HT (**canonical for height**)

- **Example models:**
  - `H_2020_h.R` (listed one per line).

### BMI (**canonical for body-mass index**)

- **Example models:** `B_2021_b.R` (b); `D_2021_d.R` (d). Reproduce it with `B_2021_b.R`.
"""


class TestUnionIsIdempotent:
    """R3: re-emitting a line the union adds nothing to must leave it byte for byte.

    The emitter used to rebuild every line as ", ".join(entries) + ".". Where
    an annotation opens a "(" it never closes, the parse ran to the end of the
    line, so the annotation took the final full stop and one more was appended:
    one per round, until three lines of nlmixr2lib's register ended in 13 or
    more. It also turned "; " into ", " and dropped prose after the last entry.
    """

    u = load_helper("union_merge_lines")

    @pytest.mark.parametrize(
        ("body", "entries", "prose"),
        [
            (
                f"{NAGY}.............",
                [("Nagy_2017_x.R", NAGY.split(" ", 1)[1])],
                [],
            ),
            (
                "`P_1998_p.R` (effect on Ka: `ka <- exp(lka + e * X)` (29% lower; Table 4)."
                " The CL effect (oral only (paper p. 3)......................",
                [("P_1998_p.R", "(effect on Ka: `ka <- exp(lka + e * X)` (29% lower; Table 4)")],
                ["The CL effect (oral only (paper p. 3)"],
            ),
            (
                "`A.R` (a), `B.R`, `C.R` (c (nested) note). Prose after the list.",
                [("A.R", "(a)"), ("B.R", ""), ("C.R", "(c (nested) note)")],
                ["Prose after the list"],
            ),
            ("`A.R` (a)", [("A.R", "(a)")], []),
            ("`A.R` (a (b] c)..., `B.R` (b).", [("A.R", "(a (b] c)"), ("B.R", "(b)")], []),
            (
                "`A.R` -- a note. Closing remark; `B.R` -- b; see Notes.",
                [("A.R", "-- a note"), ("B.R", "-- b")],
                ["Closing remark", "see Notes"],
            ),
        ],
        ids=[
            "unclosed-then-stops",
            "unclosed-never-closed",
            "prose-after",
            "no-final-stop",
            "interval",
            "sentences",
        ],
    )
    def test_an_entry_ends_where_its_note_does(
        self, body: str, entries: list[tuple[str, str]], prose: list[str]
    ) -> None:
        """The union reads entries with the dedup's parser. An entry is its
        filename and note, up to the next entry, a ``;`` or a full stop that
        ends a sentence, wherever it stands: a closing remark belongs to the
        field. The full stops that end the field are no entry's, so their
        run never grows."""
        dd = load_helper("dedup_canonical_headers")
        examples = dd._examples(dd._Field("Example models", [f"- **Example models:** {body}"]))
        assert [(e.model, e.note) for e in examples.entries] == entries
        assert examples.prose == prose

    def test_a_run_with_nothing_to_add_changes_no_byte(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The merged branch touched the file, so every line went through the emitter."""
        repo = new_repo(tmp_path, monkeypatch, {REGISTER: TRICKY_REGISTER})
        push_task_branch(
            repo,
            "claude/age",
            {REGISTER: TRICKY_REGISTER + "\n### AGE (**canonical for age**)\n"},
            "Register AGE",
        )
        wt = consolidate(repo, ["origin/claude/age"], CONSOLIDATION)
        before = (wt / REGISTER).read_bytes()
        proc = run_helper("union_merge_lines.py", *_verifier_args(repo))
        assert (proc.returncode, proc.stdout) == (0, "")
        assert proc.stderr.endswith(f"# nothing to add; left unchanged: {wt / REGISTER}\n")
        assert (wt / REGISTER).read_bytes() == before

    def test_running_twice_is_running_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two branches add to the line with the unclosed annotation; -X theirs keeps one."""
        repo = new_repo(tmp_path, monkeypatch, {REGISTER: TRICKY_REGISTER})
        dots = f"{NAGY}............."
        for name, entry in (("p", "`P_2020_p.R` (p)"), ("q", "`Q_2021_q.R` (q)")):
            text = TRICKY_REGISTER.replace(dots, f"{dots}, {entry}")
            push_task_branch(repo, f"claude/{name}", {REGISTER: text}, f"Add {name}")
        wt = consolidate(repo, ["origin/claude/p", "origin/claude/q"], CONSOLIDATION)
        assert "P_2020_p.R" not in (wt / REGISTER).read_text(), "the fixture lost nothing"

        first = run_helper("union_merge_lines.py", *_verifier_args(repo))
        assert first.returncode == 0, first.stderr
        once = (wt / REGISTER).read_bytes()
        assert once.decode() == TRICKY_REGISTER.replace(
            dots, f"{dots}, `Q_2021_q.R` (q), `P_2020_p.R` (p)"
        )
        second = run_helper("union_merge_lines.py", *_verifier_args(repo))
        assert second.returncode == 0, second.stderr
        assert second.stderr.endswith(f"# nothing to add; left unchanged: {wt / REGISTER}\n")
        assert (wt / REGISTER).read_bytes() == once


class TestUnionEditsFieldsInPlace:
    """The union reads each Example-models field whole, with the dedup's
    parser, and edits it where it stands: prose before, between and after
    the entries stays where it is, an added entry takes none of the last
    entry's words, and a model listed twice stays listed twice."""

    HEAD = "## Body size\n\n### WT (**canonical for weight**)\n\n"

    def _merge(self, base: str, *branches: str, current: str | None = None) -> Any:
        """What emit_merged makes of ``current`` (the base by default), with
        each branch cut from the base."""
        u = load_helper("union_merge_lines")
        base_text = self.HEAD + base
        fork = u.entry_map(base_text)
        union = u.collect_entries(base_text, [(self.HEAD + b, fork) for b in branches])
        return u.emit_merged(self.HEAD + (base if current is None else current), union)

    @pytest.mark.parametrize(
        ("base", "branch", "result"),
        [
            (
                "- **Example models:** Fits: `A.R` for the base case; `B.R` for the chain case.\n",
                "- **Example models:** Fits: `A.R` for the base case; `B.R` for the chain case,"
                " `C.R` (new).\n",
                "- **Example models:** Fits: `A.R` for the base case; `B.R` for the chain case,"
                " `C.R` (new).\n",
            ),
            (
                "- **Example models:** `A.R` (a). Set to 0 to simulate the drug effect only.\n",
                "- **Example models:** `A.R` (a), `B.R` (b). Set to 0 to simulate the drug effect"
                " only.\n",
                "- **Example models:** `A.R` (a), `B.R` (b). Set to 0 to simulate the drug effect"
                " only.\n",
            ),
            (
                "- **Example models:** `A.R` (first), `A.R` (second, longer).\n",
                "- **Example models:** `A.R` (first), `A.R` (second, longer), `B.R` (b).\n",
                "- **Example models:** `A.R` (first), `A.R` (second, longer), `B.R` (b).\n",
            ),
            (
                "- **Example models:**\n  - `A.R` -- a.\n",
                "- **Example models:**\n  - `A.R` -- a.\n  - `B.R` -- b.\n",
                "- **Example models:**\n  - `A.R` -- a.\n  - `B.R` -- b\n",
            ),
            (
                "- **Example models:** `A.R` (a),\n  `B.R` (b).\n",
                "- **Example models:** `A.R` (a),\n  `B.R` (b), `C.R` (c).\n",
                "- **Example models:** `A.R` (a),\n  `B.R` (b), `C.R` (c).\n",
            ),
        ],
        ids=["prose-around-entries", "closing-remark", "listed-twice", "sub-bullets", "wrapped"],
    )
    def test_a_lost_entry_comes_back_and_nothing_else_moves(
        self, base: str, branch: str, result: str
    ) -> None:
        emitted = self._merge(base, branch)
        assert (emitted.text, emitted.hand) == (self.HEAD + result, [])

    def _thrice(self, base: str, branches: list[str], current: str) -> Any:
        """emit_merged run once, and checked against twice and three times."""
        u = load_helper("union_merge_lines")
        base_text = self.HEAD + base
        fork = u.entry_map(base_text)
        union = u.collect_entries(base_text, [(self.HEAD + b, fork) for b in branches])
        once = u.emit_merged(self.HEAD + current, union)
        twice = u.emit_merged(once.text, union)
        thrice = u.emit_merged(twice.text, union)
        assert once.text == twice.text == thrice.text
        return once

    def test_a_closing_remark_after_a_run_on_note_stays(self) -> None:
        """The remark belongs to the field, not to B's note: a branch that put
        C after B neither shortened B's note nor took the remark away."""
        base = (
            "- **Example models:** `A_2020_a.R` (a), `B_2021_b.R` -- b-note."
            " Closing remark about all of them.\n"
        )
        branch = base.replace("b-note.", "b-note, `C_2022_c.R` (c).")
        emitted = self._thrice(base, [branch], base)
        assert emitted.text == self.HEAD + branch
        assert emitted.hand == []

    @pytest.mark.parametrize(
        ("base", "branch", "result"),
        [
            (
                "- **Example models:** `A.R` (note). Closing remark.\n",
                "- **Example models:**\n  - `A.R` (note). Extra sentence.\n",
                "- **Example models:** `A.R` (note). Closing remark.\n",
            ),
            (
                "- **Example models:** `A.R` (a). Closing remark.\n",
                "- **Example models:** `A.R` (a) (more words). Closing remark.\n",
                "- **Example models:** `A.R` (a) (more words). Closing remark.\n",
            ),
            (
                "- **Example models:** `A.R` (a), `B.R` (b). Closing remark.\n",
                "- **Example models:** `A.R` (a), `B.R` (b) -- extra words. Closing remark.\n",
                "- **Example models:** `A.R` (a), `B.R` (b) -- extra words. Closing remark.\n",
            ),
            (
                "- **Example models:** `A.R` (a).\n",
                "- **Example models:** `A.R` (a).\n  - `N.R` -- why it is here\n",
                "- **Example models:** `A.R` (a), `N.R` (why it is here).\n",
            ),
        ],
        ids=["bullet-sentence", "longer-note", "run-on-note", "bullet-form"],
    )
    def test_running_twice_or_three_times_is_running_once(
        self, base: str, branch: str, result: str
    ) -> None:
        """A note is compared by what it says, inline or in a sub-bullet, and a
        closing remark is no note's: nothing grows, doubles or flips form.
        Prose a branch added that the field lacks goes to the report."""
        emitted = self._thrice(base, [branch], base)
        assert emitted.text == self.HEAD + result
        set_aside = [d.dropped for h in emitted.hand for d in h.differences]
        assert set_aside == ([["Extra sentence"]] if "Extra" in branch else [])

    def test_two_copies_of_a_block_trade_entries_and_keep_one_closing_remark(self) -> None:
        """FVIII_BL on 2026-10-23: two branches registered it, one inline with a
        closing remark, one with sub-bullets, and both copies survived the
        merge. Each copy gains the other's models; the remark stays once."""
        u = load_helper("union_merge_lines")
        head = "## Coagulation\n\n### FVIII_BL (**canonical for baseline FVIII**)\n"
        inline = (
            head + "- **Example models:** `deJager_2020_factorviii.R` (observation"
            " `Cc = central / vc + FVIII_BL`; Supplement 1 Eq. 2). Set to 0 to simulate the"
            " drug-attributable FVIII only.\n"
        )
        listed = (
            head + "- **Example models:**\n  - `Bukkems_2022_factorviii.R` (observation; Eq. 2)."
            " `Bukkems_2022_factorviii_thrombinPeak.R` also drives its Emax with it.\n"
        )
        base = "## Coagulation\n\n### PLT (**canonical for platelets**)\n- **Units:** 1/nL\n"
        union = u.collect_entries(
            base, [(base + inline, u.entry_map(base)), (base + listed, u.entry_map(base))]
        )
        current = base + inline + "\n" + listed
        once = u.emit_merged(current, union)
        assert u.emit_merged(u.emit_merged(once.text, union).text, union).text == once.text
        assert once.text == (
            base
            + inline.replace(
                "Eq. 2). Set",
                "Eq. 2), `Bukkems_2022_factorviii.R` (observation; Eq. 2),"
                " `Bukkems_2022_factorviii_thrombinPeak.R` also drives its Emax with it. Set",
            )
            + "\n"
            + listed
            + "  - `deJager_2020_factorviii.R` (observation `Cc = central / vc + FVIII_BL`;"
            " Supplement 1 Eq. 2)\n"
        )
        assert once.text.count("Set to 0 to simulate") == 1
        assert once.hand == []

    def test_a_branch_model_for_a_field_that_lists_none_goes_to_the_report(self) -> None:
        base = "- **Example models:** none yet.\n"
        branch = "- **Example models:** `N.R` (n).\n"
        emitted = self._thrice(base, [branch], base)
        assert emitted.text == self.HEAD + base
        assert [(d.what, d.dropped) for h in emitted.hand for d in h.differences] == [
            ("Example models entry for N.R", ["`N.R` (n)"])
        ]

    def test_a_note_held_only_inside_a_longer_word_is_reported(self) -> None:
        """Two branches annotate N differently. The longer note holds the
        shorter one only inside a longer number ("120" in "1200"), so the
        shorter says something the kept one does not and goes to the report."""
        base = "- **Example models:** `A.R` (a).\n"
        longer = base.replace("(a).", "(a), `N.R` (CrCL capped at 1200).")
        shorter = base.replace("(a).", "(a), `N.R` (CrCL capped at 120).")
        emitted = self._thrice(base, [longer, shorter], shorter)
        assert emitted.text == self.HEAD + longer
        assert [(d.what, d.dropped) for h in emitted.hand for d in h.differences] == [
            ("Example models entry for N.R", ["`N.R` (CrCL capped at 120)"])
        ]

    def test_a_corrected_note_in_a_wrapped_field_keeps_the_line_breaks(self) -> None:
        base = "- **Example models:** `A.R` (a: 12 mg),\n  `B.R` (b).\n"
        corrected = base.replace("12 mg", "21 mg")
        assert self._thrice(base, [corrected], base).text == self.HEAD + corrected

    def test_a_change_of_form_alone_is_no_correction(self) -> None:
        """claude/move only moves A to a sub-bullet; claude/fix corrects its dose.
        The correction is the one that counts, and nothing is reported."""
        base = "- **Example models:** `A.R` (a: 12 mg), `C.R` (c).\n"
        moved = "- **Example models:** `C.R` (c).\n  - `A.R` -- a: 12 mg\n"
        fixed = base.replace("12 mg", "21 mg")
        emitted = self._thrice(base, [moved, fixed], base)
        assert emitted.text == self.HEAD + fixed
        assert not any("correct" in note for note in emitted.notes)

    def test_prose_a_branch_repeats_is_counted(self) -> None:
        """claude/b's new entry ends with the sentence A's has; the result holds
        it once, the branch twice, so the second is reported."""
        base = "- **Example models:** `A.R` (a). See Notes.\n"
        branch = base + "  - `B.R` (b). See Notes.\n"
        emitted = self._thrice(base, [branch], base)
        assert (
            emitted.text == self.HEAD + "- **Example models:** `A.R` (a), `B.R` (b). See Notes.\n"
        )
        assert [d.dropped for h in emitted.hand for d in h.differences] == [["See Notes"]]

    def test_prose_after_a_removed_entry_stays(self) -> None:
        """It is the field's, not the removed entry's."""
        base = "- **Example models:** `Old.R` (old); see the table, `C.R` (c).\n"
        removed = "- **Example models:** see the table, `C.R` (c).\n"
        emitted = self._thrice(base, [removed], base)
        assert emitted.text == self.HEAD + removed

    def test_a_field_whose_own_entries_all_went_takes_the_others(self) -> None:
        """The merged copy lists only Old, which a branch removed; K, which the
        base has, comes back with no ", " left before it."""
        base = "- **Example models:** `Old.R` (old), `K.R` (k).\n"
        removed = "- **Example models:** `K.R` (k).\n"
        stale = "- **Example models:** `Old.R` (old).\n"
        emitted = self._merge(base, removed, current=stale)
        assert emitted.text == self.HEAD + removed

    def test_a_removed_entry_takes_only_its_own_text(self) -> None:
        line = "- **Example models:** `A.R` (a), `Old.R` (old), `C.R` (c). Closing remark.\n"
        base = line.replace(", `C.R` (c)", "")
        removed = base.replace(", `Old.R` (old)", "")
        emitted = self._merge(base, removed, line, current=line)
        assert emitted.text == self.HEAD + line.replace("`Old.R` (old), ", "")
        assert (
            emitted.notes[-1]
            == "# dropped 1 entry a merged branch removed: Body size / WT (**canonical for weight**) / Old.R"
        )

    def test_every_note_longest_wins_sets_aside_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two branches add one model to one line in different words; -X theirs
        keeps the second. The union keeps the longer note and sends the other,
        verbatim, to the hand-union report it shares with the dedup."""
        repo = new_repo(tmp_path, monkeypatch, {REGISTER: BASE_REGISTER})
        for name, note in (("long", "(a longer note on N)"), ("short", "(short)")):
            text = _with_models(f"`N_2020_n.R` {note}")
            push_task_branch(repo, f"claude/{name}", {REGISTER: text}, f"Add N ({name})")
        wt = consolidate(repo, ["origin/claude/long", "origin/claude/short"], CONSOLIDATION)
        report = tmp_path / "hand-union.md"
        report.write_text("# Hand union needed in the union file\n")
        proc = run_helper("union_merge_lines.py", *_verifier_args(repo), "--report", report)
        assert proc.returncode == 0, proc.stderr
        assert (wt / REGISTER).read_text() == _with_models("`N_2020_n.R` (a longer note on N)")
        assert report.read_text() == (
            "# Hand union needed in the union file\n"
            f"# Hand union needed in {REGISTER}\n"
            "\n"
            "Two merged copies of the register annotate each model below differently.\n"
            "The union kept the longer annotation, or a branch's correction; the other\n"
            "is below, verbatim. Union it into the register by hand, keeping each\n"
            "source's attribution, and commit.\n"
            "\n"
            "## Body size / WT (**canonical for body weight**)\n"
            "\n"
            "### Example models entry for N_2020_n.R\n"
            "\n"
            "In the register:\n"
            "\n"
            "````\n"
            "`N_2020_n.R` (a longer note on N)\n"
            "````\n"
            "\n"
            "In a merged copy:\n"
            "\n"
            "````\n"
            "`N_2020_n.R` (short)\n"
            "````\n"
            "\n"
        )


class TestUnionHonoursWhatABranchRemovedOrCorrected:
    """A branch's own diff can remove an entry (a rename, a re-filing) or
    correct an annotation, not only add. The union read only additions: the
    base's old entry came back beside the renamed one, and a same-length or
    shorter correction lost to the base's annotation under "longest wins, the
    base's on a tie". The NEWS union had the same blind spot for reworded
    bullets (seven duplicates on the 2026-10-10 nlmixr2lib merge)."""

    LINE = "- **Example models:** `A_2020_a.R` (a: 12 mg), `Old_2019_x.R` (old)."
    BASE = f"# Covariates\n\n## Body size\n\n### WT (**canonical for weight**)\n\n{LINE}\n"

    def _world(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fixed_line: str
    ) -> tuple[Path, Path]:
        """claude/fix rewrites the line; claude/add appends to the same line, so
        -X theirs keeps add's copy and fix's change is lost from the merge."""
        repo = new_repo(tmp_path, monkeypatch, {REGISTER: self.BASE})
        push_task_branch(
            repo,
            "claude/fix",
            {REGISTER: self.BASE.replace(self.LINE, fixed_line)},
            "Fix the entry",
        )
        added = self.BASE.replace(
            "`Old_2019_x.R` (old).", "`Old_2019_x.R` (old), `C_2021_c.R` (c)."
        )
        push_task_branch(repo, "claude/add", {REGISTER: added}, "Add C 2021")
        wt = consolidate(repo, ["origin/claude/fix", "origin/claude/add"], CONSOLIDATION)
        assert fixed_line not in (wt / REGISTER).read_text(), "the fixture lost nothing"
        return repo, wt

    def test_a_renamed_entry_does_not_come_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        renamed = self.LINE.replace("`Old_2019_x.R`", "`New_2019_x.R`")
        repo, wt = self._world(tmp_path, monkeypatch, renamed)
        proc = run_helper("union_merge_lines.py", *_verifier_args(repo))
        assert proc.returncode == 0, proc.stderr
        line = next(x for x in (wt / REGISTER).read_text().splitlines() if "Example models" in x)
        assert line == (
            "- **Example models:** `A_2020_a.R` (a: 12 mg), `New_2019_x.R` (old), `C_2021_c.R` (c)."
        )
        assert (
            "# dropped 1 entry a merged branch removed:"
            " Body size / WT (**canonical for weight**) / Old_2019_x.R"
        ) in proc.stderr

    def test_a_same_length_correction_wins_over_the_base(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        corrected = self.LINE.replace("(a: 12 mg)", "(a: 21 mg)")
        repo, wt = self._world(tmp_path, monkeypatch, corrected)
        proc = run_helper("union_merge_lines.py", *_verifier_args(repo))
        assert proc.returncode == 0, proc.stderr
        line = next(x for x in (wt / REGISTER).read_text().splitlines() if "Example models" in x)
        assert line == (
            "- **Example models:** `A_2020_a.R` (a: 21 mg), `Old_2019_x.R` (old), `C_2021_c.R` (c)."
        )

    def test_a_correction_to_an_annotation_main_also_changed_does_not_override_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The branch saw "12 mg"; main has since written "15 mg". The branch's
        "21 mg" corrects text the base no longer has, so the base's stands."""
        repo = new_repo(tmp_path, monkeypatch, {REGISTER: self.BASE})
        push_task_branch(
            repo,
            "claude/fix",
            {REGISTER: self.BASE.replace("(a: 12 mg)", "(a: 21 mg)")},
            "Fix the entry",
        )
        update_main(repo, {REGISTER: self.BASE.replace("(a: 12 mg)", "(a: 15 mg)")}, "main fix")
        added = self.BASE.replace("(a: 12 mg)", "(a: 15 mg)").replace(
            "`Old_2019_x.R` (old).", "`Old_2019_x.R` (old), `C_2021_c.R` (c)."
        )
        push_task_branch(repo, "claude/add", {REGISTER: added}, "Add C 2021")
        wt = consolidate(repo, ["origin/claude/fix", "origin/claude/add"], CONSOLIDATION)
        proc = run_helper("union_merge_lines.py", *_verifier_args(repo))
        assert proc.returncode == 0, proc.stderr
        assert self._line(wt) == (
            "- **Example models:** `A_2020_a.R` (a: 15 mg), `Old_2019_x.R` (old), `C_2021_c.R` (c)."
        )

    @staticmethod
    def _line(wt: Path) -> str:
        return next(x for x in (wt / REGISTER).read_text().splitlines() if "Example models" in x)

    def _run(self, repo: Path) -> subprocess.CompletedProcess[str]:
        proc = run_helper("union_merge_lines.py", *_verifier_args(repo))
        assert proc.returncode == 0, proc.stderr
        return proc

    def test_a_rename_run_twice_is_run_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        renamed = self.LINE.replace("`Old_2019_x.R`", "`New_2019_x.R`")
        repo, wt = self._world(tmp_path, monkeypatch, renamed)
        self._run(repo)
        once = (wt / REGISTER).read_bytes()
        second = self._run(repo)
        assert second.stderr.endswith(f"# nothing to add; left unchanged: {wt / REGISTER}\n")
        assert (wt / REGISTER).read_bytes() == once

    def test_an_empty_correction_wins(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A correction that removes the annotation is still a correction."""
        repo, wt = self._world(tmp_path, monkeypatch, self.LINE.replace(" (a: 12 mg)", ""))
        self._run(repo)
        assert self._line(wt) == (
            "- **Example models:** `A_2020_a.R`, `Old_2019_x.R` (old), `C_2021_c.R` (c)."
        )

    def test_a_removal_of_an_entry_main_also_changed_is_not_honoured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The branch removed Old as it saw it; main has re-annotated Old since."""
        repo = new_repo(tmp_path, monkeypatch, {REGISTER: self.BASE})
        push_task_branch(
            repo, "claude/fix", {REGISTER: self.BASE.replace(", `Old_2019_x.R` (old)", "")}, "Drop"
        )
        main = self.BASE.replace("(old)", "(old, re-annotated)")
        update_main(repo, {REGISTER: main}, "main re-annotates Old")
        added = main.replace("(old, re-annotated).", "(old, re-annotated), `C_2021_c.R` (c).")
        push_task_branch(repo, "claude/add", {REGISTER: added}, "Add C 2021")
        wt = consolidate(repo, ["origin/claude/fix", "origin/claude/add"], CONSOLIDATION)
        proc = self._run(repo)
        assert "Old_2019_x.R` (old, re-annotated)" in self._line(wt)
        assert "dropped" not in proc.stderr

    def test_an_entry_one_branch_removed_and_another_added_stays(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """claude/early, cut before main had Old, adds it; claude/fix removes it."""
        start = self.BASE.replace(", `Old_2019_x.R` (old)", "")
        repo = new_repo(tmp_path, monkeypatch, {REGISTER: start})
        push_task_branch(
            repo,
            "claude/early",
            {REGISTER: start.replace("(a: 12 mg).", "(a: 12 mg), `Old_2019_x.R` (old).")},
            "Add Old",
        )
        update_main(repo, {REGISTER: self.BASE}, "main gains Old")
        push_task_branch(repo, "claude/fix", {REGISTER: start}, "Drop Old")
        wt = consolidate(repo, ["origin/claude/early", "origin/claude/fix"], CONSOLIDATION)
        self._run(repo)
        assert "`Old_2019_x.R` (old)" in self._line(wt)

    def test_two_branches_correcting_one_annotation_differently_keep_the_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = new_repo(tmp_path, monkeypatch, {REGISTER: self.BASE})
        for name, dose in (("a", "21 mg"), ("b", "31 mg")):
            text = self.BASE.replace("(a: 12 mg)", f"(a: {dose})")
            push_task_branch(repo, f"claude/{name}", {REGISTER: text}, f"Fix {name}")
        wt = consolidate(repo, ["origin/claude/a", "origin/claude/b"], CONSOLIDATION)
        proc = self._run(repo)
        assert "(a: 21 mg)" in self._line(wt)
        assert "# two merged branches correct" in proc.stderr

    def test_a_renamed_heading_does_not_empty_its_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """claude/fix renames the heading right above the line, so its copy has
        no entries in the old bucket; that is not a removal of them."""
        base = self.BASE.replace("**)\n\n- **Example", "**)\n- **Example")
        renamed = base.replace("### WT (**canonical for weight**)", "### WT (**weight**)")
        added = base.replace("`Old_2019_x.R` (old).", "`Old_2019_x.R` (old), `C_2021_c.R` (c).")
        repo = new_repo(tmp_path, monkeypatch, {REGISTER: base})
        push_task_branch(repo, "claude/fix", {REGISTER: renamed}, "Rename the heading")
        push_task_branch(repo, "claude/add", {REGISTER: added}, "Add C 2021")
        wt = consolidate(repo, ["origin/claude/fix", "origin/claude/add"], CONSOLIDATION)
        assert "canonical for weight" in (wt / REGISTER).read_text(), "the fixture lost nothing"
        proc = self._run(repo)
        line = self._line(wt)
        assert "`A_2020_a.R`" in line and "`Old_2019_x.R`" in line
        assert "dropped" not in proc.stderr

    def test_a_line_whose_entries_all_went_is_left_alone(self) -> None:
        """Every entry removed would leave "- **Example models:** ."; the line is
        kept as it is and reported instead."""
        u = load_helper("union_merge_lines")
        bucket = ("Body size", "WT (**canonical for weight**)")
        union = u.Union()
        union.annotations[bucket] = {"A_2020_a.R": "(a)"}
        union.base_order[bucket] = ["A_2020_a.R"]
        union.removed[bucket] = {"A_2020_a.R"}
        text = "## Body size\n\n### WT (**canonical for weight**)\n\n- **Example models:** `A_2020_a.R` (a).\n"
        emitted = u.emit_merged(text, union)
        assert emitted.text == text
        assert any("every entry" in note for note in emitted.notes)


class TestDedupCanonicalHeaders:
    DUPLICATED = """\
# Compartments

## Drugs

### col (**canonical for colistin**)

- **Example models:** `A_2020_col.R`.

### col (**canonical for colistin**)

- **Example models:** `B_2021_col.R` (with a longer annotation).

## Metabolites

### col (**suffix for a colistin metabolite**)

- **Example models:** `C_2022_col.R`.
"""
    DEDUPED = """\
# Compartments

## Drugs

### col (**canonical for colistin**)

- **Example models:** `B_2021_col.R` (with a longer annotation), `A_2020_col.R`.

## Metabolites

### col (**suffix for a colistin metabolite**)

- **Example models:** `C_2022_col.R`.
"""

    def test_collapses_a_duplicate_within_a_section(self, tmp_path: Path) -> None:
        """The longest block is kept and gains the other's example model,
        which has no annotation, so it is carried as the bare filename.

        The ``col`` under Metabolites is legitimate reuse in another section
        and stays: that is the default, per-section scope.
        """
        register = tmp_path / "compartment-names.md"
        register.write_text(self.DUPLICATED)

        proc = run_helper("dedup_canonical_headers.py", "--check", register)
        assert (proc.returncode, proc.stdout) == (1, "")
        assert proc.stderr == (
            f"# DUPLICATE canonical headers (per-##-section) in {register}: 1\n"
            "#   [Drugs] col x2 (lines [5, 9])\n"
        )
        assert register.read_text() == self.DUPLICATED, "--check must not edit"

        proc = run_helper("dedup_canonical_headers.py", register)
        assert (proc.returncode, proc.stderr) == (0, "")
        assert proc.stdout == (
            f"deduped 1 canonical name(s) in {register}:\n"
            "  col: 2 -> 1  (+examples ['A_2020_col.R'])\n"
        )
        assert register.read_text() == self.DEDUPED

        proc = run_helper("dedup_canonical_headers.py", "--check", register)
        assert (proc.returncode, proc.stdout) == (0, "")
        assert proc.stderr == f"# clean (no per-##-section duplicate canonicals): {register}\n"

    def test_global_scope_flags_reuse_across_sections(self, tmp_path: Path) -> None:
        """The covariate register's scope: one canonical, one block, file-wide."""
        register = tmp_path / "covariate-columns.md"
        register.write_text(self.DEDUPED)
        proc = run_helper("dedup_canonical_headers.py", "--global", "--check", register)
        assert (proc.returncode, proc.stdout) == (1, "")
        assert proc.stderr == (
            f"# DUPLICATE canonical headers (whole-file) in {register}: 1\n"
            "#   col x2 (lines [5, 11])\n"
        )

    @pytest.mark.parametrize("check", [True, False], ids=["--check", "fix"])
    def test_a_missing_file_is_an_error_and_nothing_is_edited(
        self, check: bool, tmp_path: Path
    ) -> None:
        """--check used to skip it and exit 0: a merge gate passing on no file."""
        register = tmp_path / "register.md"
        register.write_text(self.DUPLICATED)
        missing = tmp_path / "no-such.md"
        flags = ["--check"] if check else []
        proc = run_helper("dedup_canonical_headers.py", *flags, register, missing)
        assert (proc.returncode, proc.stdout) == (2, "")
        assert proc.stderr == f"ERROR: (dedup) no such file: {missing}\n"
        assert register.read_text() == self.DUPLICATED


class TestDedupCarriesWhatOnlyTheDroppedCopyHas:
    """Known-answer fixtures: what a dropped duplicate carries into the kept
    one, in which form, and when the two copies need a person instead."""

    @staticmethod
    def _dedup(tmp_path: Path, text: str) -> tuple[subprocess.CompletedProcess[str], str]:
        register = tmp_path / "covariate-columns.md"
        register.write_text(text)
        proc = run_helper("dedup_canonical_headers.py", "--global", register)
        return proc, register.read_text()

    def test_an_inline_entry_keeps_its_own_annotation(self, tmp_path: Path) -> None:
        text = (
            "## Body size\n\n"
            "### WT (**canonical for weight**)\n"
            "- **Description:** Body weight.\n"
            "- **Notes:** Only this copy has notes, which makes it the longest, the one kept.\n"
            "- **Example models:** `A_2020_a.R` (allometric on CL, `(WT/70)^0.75`), "
            "`B_2021_b.R` (on V).\n\n"
            "### WT (**canonical for weight**)\n"
            "- **Description:** Body weight.\n"
            "- **Example models:** `A_2020_a.R` (allometric on CL, `(WT/70)^0.75`), "
            "`C_2022_c.R` (power, `exp(0.5 * log(WT / 70))`), `D_2023_d.R`.\n"
        )
        proc, out = self._dedup(tmp_path, text)
        assert (proc.returncode, proc.stderr) == (0, "")
        assert out == (
            "## Body size\n\n"
            "### WT (**canonical for weight**)\n"
            "- **Description:** Body weight.\n"
            "- **Notes:** Only this copy has notes, which makes it the longest, the one kept.\n"
            "- **Example models:** `A_2020_a.R` (allometric on CL, `(WT/70)^0.75`), "
            "`B_2021_b.R` (on V), `C_2022_c.R` (power, `exp(0.5 * log(WT / 70))`), "
            "`D_2023_d.R`.\n"
        )
        assert "merge dedup" not in out

    def test_an_empty_list_style_line_gets_sub_bullets(self, tmp_path: Path) -> None:
        """The label alone, with nothing after it: entries go under it as
        sub-bullets, not after the label as ", `X.R`"."""
        text = (
            "### pk (**canonical for a PK compartment**)\n"
            "- **Role:** state\n"
            "- **Example models:**\n"
            "- **Notes:** Only this copy has notes, which makes it the longest, the one kept.\n\n"
            "### pk (**canonical for a PK compartment**)\n"
            "- **Role:** state\n"
            "- **Example models:** `E_2024_e.R` (two-compartment).\n"
        )
        proc, out = self._dedup(tmp_path, text)
        assert (proc.returncode, proc.stderr) == (0, "")
        assert out == (
            "### pk (**canonical for a PK compartment**)\n"
            "- **Role:** state\n"
            "- **Example models:**\n"
            "  - `E_2024_e.R` (two-compartment)\n"
            "- **Notes:** Only this copy has notes, which makes it the longest, the one kept.\n"
        )

    def test_list_and_inline_forms_meet(self, tmp_path: Path) -> None:
        """A list-style keeper gains sub-bullets; an inline keeper gains a
        list entry as ``(text)``."""
        listed = (
            "### Z (**canonical for z**)\n"
            "- **Example models:**\n"
            "  - `A_2020_a.R` -- first, with a long description of the model.\n\n"
            "### Z (**canonical for z**)\n"
            "- **Example models:** `B_2021_b.R` (second).\n"
        )
        proc, out = self._dedup(tmp_path, listed)
        assert proc.returncode == 0
        assert out == (
            "### Z (**canonical for z**)\n"
            "- **Example models:**\n"
            "  - `A_2020_a.R` -- first, with a long description of the model.\n"
            "  - `B_2021_b.R` (second)\n"
        )

        inline = (
            "### Y (**canonical for y**)\n"
            "- **Example models:** `A_2020_a.R` (first, with a long description of the model).\n\n"
            "### Y (**canonical for y**)\n"
            "- **Example models:**\n"
            "  - `B_2021_b.R` -- second.\n"
        )
        proc, out = self._dedup(tmp_path, inline)
        assert proc.returncode == 0
        assert out == (
            "### Y (**canonical for y**)\n"
            "- **Example models:** `A_2020_a.R` (first, with a long description of the model), "
            "`B_2021_b.R` (second).\n"
        )

    def test_source_alias_sub_bullets_are_unioned(self, tmp_path: Path) -> None:
        text = (
            "### SEX (**canonical for sex**)\n"
            "- **Source aliases:**\n"
            "  - `GENDER` -- used in `A_2020_a.R`, which codes 1 = female and 0 = male.\n"
            "- **Example models:** `A_2020_a.R`.\n"
            "- **Notes:** Only this copy has notes, which makes it the longest, the one kept.\n\n"
            "### SEX (**canonical for sex**)\n"
            "- **Source aliases:**\n"
            "  - `GENDER` -- used in `A_2020_a.R`, which codes 1 = female and 0 = male.\n"
            "  - `MALE` -- inverted; used in `B_2021_b.R`.\n"
            "- **Example models:** `B_2021_b.R`.\n"
        )
        proc, out = self._dedup(tmp_path, text)
        assert proc.returncode == 0
        assert proc.stdout.endswith(
            "  SEX: 2 -> 1  (+examples ['B_2021_b.R']; +1 source alias(es))\n"
        )
        assert out == (
            "### SEX (**canonical for sex**)\n"
            "- **Source aliases:**\n"
            "  - `GENDER` -- used in `A_2020_a.R`, which codes 1 = female and 0 = male.\n"
            "  - `MALE` -- inverted; used in `B_2021_b.R`.\n"
            "- **Example models:** `A_2020_a.R`, `B_2021_b.R`.\n"
            "- **Notes:** Only this copy has notes, which makes it the longest, the one kept.\n"
        )

    @pytest.mark.parametrize(
        ("kept", "dropped", "fields"),
        [
            ("- **Notes:** First note, the longer one.", "- **Notes:** Other note.", "Notes"),
            ("- **Role:** state, the longer one.", "- **Role:** output", "Role"),
            ("- **Units:** kg, the longer one.", "", None),
        ],
        ids=["notes", "role", "keeper-only-field"],
    )
    def test_prose_that_differs_is_left_for_a_hand_union(
        self, tmp_path: Path, kept: str, dropped: str, fields: str | None
    ) -> None:
        """Nothing is discarded silently: a group whose dropped copy says
        something the keeper words differently is left whole, reported, and
        exit 3; the other group in the file is still collapsed. A field only
        the keeper has loses nothing, so that group collapses."""
        group = (
            "### Q (**canonical for q**)\n"
            f"{kept}\n"
            "- **Example models:** `A_2020_a.R`.\n\n"
            "### Q (**canonical for q**)\n"
            + (f"{dropped}\n" if dropped else "")
            + "- **Example models:** `B_2021_b.R`.\n\n"
        )
        other = (
            "### R (**canonical for r**)\n"
            "- **Example models:** `C_2022_c.R`.\n\n"
            "### R (**canonical for r**)\n"
            "- **Example models:** `D_2023_d.R`.\n"
        )
        proc, out = self._dedup(tmp_path, group + other)
        register = tmp_path / "covariate-columns.md"
        collapsed_other = (
            "### R (**canonical for r**)\n- **Example models:** `C_2022_c.R`, `D_2023_d.R`.\n"
        )
        if fields is None:
            assert (proc.returncode, proc.stderr) == (0, "")
            assert out == (
                "### Q (**canonical for q**)\n"
                f"{kept}\n"
                "- **Example models:** `A_2020_a.R`, `B_2021_b.R`.\n\n" + collapsed_other
            )
            return
        assert proc.returncode == 3
        assert proc.stderr == (
            f"# HAND UNION NEEDED in {register}: ### Q (lines [1, 5]): the copies differ in "
            f"{fields}; left as they are, merge them by hand\n"
        )
        assert out == group + collapsed_other
        check = run_helper("dedup_canonical_headers.py", "--global", "--check", register)
        assert check.returncode == 1

    def test_a_different_heading_is_two_meanings(self, tmp_path: Path) -> None:
        text = (
            "### col (**canonical for colistin**)\n"
            "- **Example models:** `A_2020_a.R`.\n\n"
            "### col (**canonical for colestipol**)\n"
            "- **Example models:** `B_2021_b.R`.\n"
        )
        proc, out = self._dedup(tmp_path, text)
        register = tmp_path / "covariate-columns.md"
        assert (proc.returncode, out) == (3, text)
        assert proc.stderr == (
            f"# HAND UNION NEEDED in {register}: ### col (lines [1, 4]): the copies differ in "
            "heading; left as they are, merge them by hand\n"
        )


HISTORY = Path(__file__).resolve().parent.parent / "fixtures" / "registers" / "dedup_history"
KEPT_NOTES = "- **Notes:** Only this copy has notes, which makes it the longest, the one kept.\n"


def _pair(kept: str, dropped: str) -> str:
    """Two copies of ``### Q``; the first, with KEPT_NOTES, is the longest."""
    head = "### Q (**canonical for q**)\n"
    return f"{head}{kept}{KEPT_NOTES}\n{head}{dropped}"


def _blocks(dd: ModuleType, text: str) -> dict[str, list[Any]]:
    """Every ``### NAME`` block of ``text``, by name, parsed by the dedup itself."""
    lines = text.split("\n")
    heads, bounds = dd._parse(lines)
    out: dict[str, list[Any]] = defaultdict(list)
    for i, name, _ in heads:
        out[name].append(dd._parse_block(lines[i : dd._block_end(i, bounds)]))
    return out


def _entries(dd: ModuleType, block: Any) -> list[Any]:
    return [e for f in block.named("Example models") for e in dd._examples(f).entries]


SHAPES = HISTORY.parent / "dedup_shapes"
WORD = re.compile(r"[A-Za-z0-9_.\-]+")
UNCOUNTED = frozenset({"and", "or", "plus", "none"})
"""Words a dedup may drop: what joins two entries, and the "none" of a field that lists nothing."""
FIELD_LINE = re.compile(r"^- \*\*[^*]+:\*\*")


def _words(text: str) -> set[str]:
    return {w.strip(".-") for w in WORD.findall(text)} - {""}


def _plain_blocks(text: str) -> dict[str, list[str]]:
    """Each ``### NAME`` block's text, by NAME, split on headings alone: the
    oracle reads the register without the dedup's parser."""
    out: dict[str, list[str]] = defaultdict(list)
    name, lines = None, []
    for line in [*text.split("\n"), "# end"]:
        if line.startswith("#"):
            if name is not None:
                out[name].append("\n".join(lines))
            name, lines = (line.split()[1], [line]) if line.startswith("### ") else (None, [])
        elif name is not None:
            lines.append(line)
    return out


def _example_text(block: str) -> str:
    """The text of a block's Example-models fields, up to the next field."""
    out, inside = [], False
    for line in block.split("\n"):
        if FIELD_LINE.match(line):
            inside = line.startswith("- **Example models:**")
        if inside:
            out.append(line)
    return "\n".join(out)


class TestDedupKeepsEveryModelAndWord:
    """With ``--report``, as merge_branches.sh runs it, every group collapses.

    Every model a dropped copy lists as an Example-models entry is then an
    entry of the kept block; a mention inside another entry's annotation is
    no entry. Every word a dropped copy says is in the register or, verbatim,
    in the hand-union report, and the report repeats nothing the register
    already says.
    """

    @staticmethod
    def _dedup(
        tmp_path: Path, text: str, *scope: str
    ) -> tuple[subprocess.CompletedProcess[str], str, str]:
        register = tmp_path / "covariate-columns.md"
        report = tmp_path / "hand-union.md"
        register.write_text(text)
        proc = run_helper(
            "dedup_canonical_headers.py", *(scope or ("--global",)), "--report", report, register
        )
        return proc, register.read_text(), report.read_text() if report.exists() else ""

    @pytest.mark.parametrize(
        ("kept", "dropped", "result", "carried"),
        [
            (
                "`A_2020_a.R` (x).",
                " `A_2020_a.R` and `B_2021_b.R` (y).",
                "`A_2020_a.R` (x), `B_2021_b.R` (y).",
                ["B_2021_b.R"],
            ),
            (
                "`A_2020_a.R` (x).",
                " `A_2020_a.R` (x); `B_2021_b.R` (y).",
                "`A_2020_a.R` (x), `B_2021_b.R` (y).",
                ["B_2021_b.R"],
            ),
            (
                "`A_2020_a.R` (refits the `B_2021_b.R` data).",
                " `B_2021_b.R` (original).",
                "`A_2020_a.R` (refits the `B_2021_b.R` data), `B_2021_b.R` (original).",
                ["B_2021_b.R"],
            ),
            (
                "`A_2020_a.R` (x).",
                " `B_2021_b.R` (after `C_2022_c.R`).",
                "`A_2020_a.R` (x), `B_2021_b.R` (after `C_2022_c.R`).",
                ["B_2021_b.R"],
            ),
            (
                "`A_2020_a.R` (x).",
                "\n  * `B_2021_b.R` -- y.\n  * `C_2022_c.R`",
                "`A_2020_a.R` (x), `B_2021_b.R` (y), `C_2022_c.R`.",
                ["B_2021_b.R", "C_2022_c.R"],
            ),
        ],
        ids=["and", "semicolon", "keeper-mentions-it", "dropped-mentions-one", "star-bullets"],
    )
    def test_every_model_listed_as_an_entry_stays_an_entry(
        self, tmp_path: Path, kept: str, dropped: str, result: str, carried: list[str]
    ) -> None:
        """A model joined by ``and`` or ``;`` was read as part of the entry
        before it and lost; a model the keeper names only inside an
        annotation was read as one it already lists."""
        text = _pair(f"- **Example models:** {kept}\n", f"- **Example models:**{dropped}\n")
        proc, out, report = self._dedup(tmp_path, text)
        assert (proc.returncode, proc.stderr, report) == (0, "", "")
        assert out == f"### Q (**canonical for q**)\n- **Example models:** {result}\n{KEPT_NOTES}"
        assert proc.stdout.endswith(f"  Q: 2 -> 1  (+examples {carried})\n")

    @pytest.mark.parametrize(
        ("kept", "result"),
        [
            (
                "`A_2020_a.R` (founding example). Set to 0 to simulate the drug effect only.",
                "`A_2020_a.R` (founding example), `B_2021_b.R` (second). "
                "Set to 0 to simulate the drug effect only.",
            ),
            (
                "`A_2020_a.R` (x); see Notes.",
                "`A_2020_a.R` (x), `B_2021_b.R` (second); see Notes.",
            ),
            (
                "`A_2020_a.R` -- the founding example.",
                "`A_2020_a.R` -- the founding example, `B_2021_b.R` (second).",
            ),
            ("`A_2020_a.R`", "`A_2020_a.R`, `B_2021_b.R` (second)"),
        ],
        ids=["prose-after-a-full-stop", "prose-after-a-semicolon", "run-on-entry", "no-stop"],
    )
    def test_carried_entries_go_after_the_last_entry(
        self, tmp_path: Path, kept: str, result: str
    ) -> None:
        """Prose that closes the keeper's list stays last."""
        text = _pair(
            f"- **Example models:** {kept}\n", "- **Example models:** `B_2021_b.R` (second).\n"
        )
        proc, out, report = self._dedup(tmp_path, text)
        assert (proc.returncode, proc.stderr, report) == (0, "", "")
        assert out == f"### Q (**canonical for q**)\n- **Example models:** {result}\n{KEPT_NOTES}"

    def test_whitespace_tabs_and_carriage_returns_are_no_difference(self, tmp_path: Path) -> None:
        """A CRLF file also keeps CRLF on the lines the dedup writes."""
        text = _pair(
            "- **Description:** Body weight, in kg.\n- **Example models:** `A_2020_a.R` (x).\n",
            "- **Description:**\tBody  weight,\tin kg. \n- **Example models:** `B_2021_b.R`.\n",
        )
        register = tmp_path / "covariate-columns.md"
        report = tmp_path / "hand-union.md"
        register.write_bytes(text.replace("\n", "\r\n").encode())
        proc = run_helper("dedup_canonical_headers.py", "--global", "--report", report, register)
        assert (proc.returncode, proc.stderr, report.exists()) == (0, "", False)
        assert register.read_bytes().decode() == (
            "### Q (**canonical for q**)\r\n"
            "- **Description:** Body weight, in kg.\r\n"
            "- **Example models:** `A_2020_a.R` (x), `B_2021_b.R`.\r\n"
            f"{KEPT_NOTES.rstrip()}\r\n"
        )

    def test_dropping_the_last_block_keeps_the_final_line_break(self, tmp_path: Path) -> None:
        """With no blank line between the copies, the file still ends with a line break."""
        example = "- **Example models:** `A_2020_a.R`.\n"
        text = _pair(example, example).replace(f"{KEPT_NOTES}\n", KEPT_NOTES)
        proc, out, _ = self._dedup(tmp_path, text)
        assert proc.returncode == 0
        assert out == f"### Q (**canonical for q**)\n{example}{KEPT_NOTES}"

    @pytest.mark.parametrize(
        ("kept", "dropped", "result"),
        [
            (
                "- **Source aliases:** none.\n",
                "- **Source aliases:**\n  - `GENDER` -- 1 = female.\n",
                "- **Source aliases:**\n  - `GENDER` -- 1 = female.\n",
            ),
            (
                "- **Source aliases:**\n  - `GENDER` -- 1 = female.\n",
                "- **Source aliases:** None.\n",
                "- **Source aliases:**\n  - `GENDER` -- 1 = female.\n",
            ),
            (
                "- **Source aliases:** none.\n",
                "- **Source aliases:** none\n",
                "- **Source aliases:** none.\n",
            ),
        ],
        ids=["kept-none", "dropped-none", "both-none"],
    )
    def test_source_aliases_none_lists_nothing(
        self, tmp_path: Path, kept: str, dropped: str, result: str
    ) -> None:
        text = _pair(
            f"{kept}- **Example models:** `A_2020_a.R`.\n",
            f"{dropped}- **Example models:** `A_2020_a.R`.\n",
        )
        proc, out, report = self._dedup(tmp_path, text)
        assert (proc.returncode, proc.stderr, report) == (0, "", "")
        assert out == (
            f"### Q (**canonical for q**)\n{result}- **Example models:** `A_2020_a.R`.\n{KEPT_NOTES}"
        )

    def test_a_repeated_field_is_compared_as_written(self, tmp_path: Path) -> None:
        """Two identical copies that each repeat a field collapse; a repeat
        that differs is reported beside every field of that name."""
        block = (
            "### Q (**canonical for q**)\n"
            "- **Notes:** First.\n"
            "- **Example models:** `A_2020_a.R`.\n"
            "- **Notes:** Second, after the models.\n"
        )
        proc, out, report = self._dedup(tmp_path, f"{block}\n{block}")
        assert (proc.returncode, proc.stderr, report) == (0, "", "")
        assert out == block

        differing = block.replace("Second, after the models.", "Second, worded otherwise.")
        proc, out, report = self._dedup(tmp_path, f"{block}\n{differing}")
        assert proc.returncode == 0
        assert out == block
        assert (
            "### Notes\n\nIn the register:\n\n````\n"
            "- **Notes:** First.\n- **Notes:** Second, after the models.\n````\n\n"
            "In a dropped copy:\n\n````\n- **Notes:** Second, worded otherwise.\n````\n"
        ) in report

    def test_what_the_register_lacks_goes_to_the_report_verbatim(self, tmp_path: Path) -> None:
        """A different annotation for a model both copies list, prose in the
        dropped Example models, and a differing field: the register keeps its
        own wording and gains only the dropped copy's other entry."""
        text = _pair(
            "- **Description:** Q, as the copy kept describes it.\n"
            "- **Units:** kg\n- **Example models:** `A_2020_a.R` (terse).\n",
            "- **Units:** kilograms\n"
            "- **Example models:** Fits of 3-compartment extractions: "
            "`A_2020_a.R` (fitted Emax 1.2, Table 6); `B_2021_b.R` for the chain case.\n",
        )
        proc, out, report = self._dedup(tmp_path, text)
        register = tmp_path / "covariate-columns.md"
        assert proc.returncode == 0
        assert proc.stderr == (
            f"# HAND UNION NEEDED in {register}: ### Q: the copies differ in Units, "
            "Example models, Example models entry for A_2020_a.R; collapsed, the dropped "
            "wording is in "
            f"{tmp_path / 'hand-union.md'}\n"
        )
        assert out == (
            "### Q (**canonical for q**)\n"
            "- **Description:** Q, as the copy kept describes it.\n"
            "- **Units:** kg\n"
            "- **Example models:** `A_2020_a.R` (terse), `B_2021_b.R` for the chain case.\n"
            f"{KEPT_NOTES}"
        )
        assert report == (
            f"# Hand union needed in {register}\n"
            "\n"
            "Each canonical below was registered more than once. The register keeps\n"
            "one copy (the one the base has, else the longest); what a dropped copy\n"
            "said differently is below, verbatim. Union it into the register by hand,\n"
            "keeping each source's attribution, and commit.\n"
            "\n"
            "## Q (lines [1, 7] before the dedup)\n"
            "\n"
            "### Units\n"
            "\n"
            "In the register:\n"
            "\n"
            "````\n"
            "- **Units:** kg\n"
            "````\n"
            "\n"
            "In a dropped copy:\n"
            "\n"
            "````\n"
            "- **Units:** kilograms\n"
            "````\n"
            "\n"
            "### Example models\n"
            "\n"
            "In the register:\n"
            "\n"
            "````\n"
            "- **Example models:** `A_2020_a.R` (terse).\n"
            "````\n"
            "\n"
            "In a dropped copy:\n"
            "\n"
            "````\n"
            "Fits of 3-compartment extractions\n"
            "````\n"
            "\n"
            "### Example models entry for A_2020_a.R\n"
            "\n"
            "In the register:\n"
            "\n"
            "````\n"
            "`A_2020_a.R` (terse)\n"
            "````\n"
            "\n"
            "In a dropped copy:\n"
            "\n"
            "````\n"
            "`A_2020_a.R` (fitted Emax 1.2, Table 6)\n"
            "````\n"
            "\n"
        )
        check = run_helper("dedup_canonical_headers.py", "--global", "--check", register)
        assert check.returncode == 0

    @pytest.mark.parametrize(
        ("kept", "dropped", "result"),
        [
            (
                "- **Example models:** `A_2020_a.R` (a).\n",
                "- **Example models:** `B_2021_b.R` (b).\n  - `C_2022_c.R` -- under the list.\n",
                "- **Example models:** `A_2020_a.R` (a), `B_2021_b.R` (b),"
                " `C_2022_c.R` (under the list).\n",
            ),
            (
                "- **Example models:** `A_2020_a.R` (a).\n",
                "- **Example models:** `B_2021_b.R` (b),\n  `C_2022_c.R` (c).\n",
                "- **Example models:** `A_2020_a.R` (a), `B_2021_b.R` (b), `C_2022_c.R` (c).\n",
            ),
            (
                "- **Example models:** `A_2020_a.R` (a).\n",
                "- **Example models:**\n  `B_2021_b.R` is the chain case.\n",
                "- **Example models:** `A_2020_a.R` (a), `B_2021_b.R` is the chain case.\n",
            ),
            (
                "- **Example models:** `A_2020_a.R` (a).\n  * `C_2022_c.R` -- under the list.\n",
                "- **Example models:** `B_2021_b.R` (b).\n",
                "- **Example models:** `A_2020_a.R` (a).\n  * `C_2022_c.R` -- under the list.\n"
                "  * `B_2021_b.R` (b)\n",
            ),
            (
                "- **Example models:** `A_2020_a.R` (a),\n  `B_2021_b.R` (b).\n",
                "- **Example models:** `B_2021_b.R`, `C_2022_c.R` (c).\n",
                "- **Example models:** `A_2020_a.R` (a),\n  `B_2021_b.R` (b), `C_2022_c.R` (c).\n",
            ),
            (
                "- **Example models:**\n  * `A_2020_a.R` -- a.\n\n",
                "- **Example models:** `B_2021_b.R` (b).\n",
                "- **Example models:**\n  * `A_2020_a.R` -- a.\n  * `B_2021_b.R` (b)\n\n",
            ),
        ],
        ids=[
            "sub-bullet-under-inline",
            "wrapped",
            "label-then-text",
            "keeper-bullets-last",
            "keeper-wrapped",
            "bullets-then-blank",
        ],
    )
    def test_the_whole_field_is_read(
        self, tmp_path: Path, kept: str, dropped: str, result: str
    ) -> None:
        """The label line, the lines that continue it and the sub-bullets,
        under an inline list too: C_2022_c.R under the dropped copy's list is
        carried, and a wrapped keeper lists B_2021_b.R once and takes the new
        entry after its real last one."""
        proc, out, report = self._dedup(tmp_path, _pair(kept, dropped))
        assert (proc.returncode, proc.stderr, report) == (0, "", "")
        assert out == f"### Q (**canonical for q**)\n{result}{KEPT_NOTES}"

    def test_source_alias_text_outside_a_bullet_is_kept_or_reported(self, tmp_path: Path) -> None:
        """The keeper's text under its label stays when the dropped copy's
        bullets are added, and a dropped copy's text outside a bullet goes to
        the report."""
        kept = "- **Source aliases:**\n  Read from the dataset's own header.\n"
        dropped = "- **Source aliases:**\n  Named by the sponsor.\n  - `BW` -- weight.\n"
        proc, out, report = self._dedup(tmp_path, _pair(kept, dropped))
        assert proc.returncode == 0
        assert out == (
            "### Q (**canonical for q**)\n- **Source aliases:**\n"
            "  Read from the dataset's own header.\n  - `BW` -- weight.\n" + KEPT_NOTES
        )
        assert "In a dropped copy:\n\n````\n  Named by the sponsor.\n````\n" in report

    @pytest.mark.parametrize(
        ("body", "models"),
        [
            (f"{NAGY}............., `B_2021_b.R` (b).", ["Nagy_2017_x.R", "B_2021_b.R"]),
            ("`A.R` (a (b] c)..., `B.R` (b).", ["A.R", "B.R"]),
            ("`A.R` (unclosed note, `B.R` (b).", ["A.R", "B.R"]),
            ("`A.R` (+28.5% (4, 5], +33.3% (5, 6] h; `T_CONMED`), `B.R`.", ["A.R", "B.R"]),
            ("`A.R` (refits `B.R`; window (4, 5] h), `C.R`.", ["A.R", "C.R"]),
            ("`A.R` (uses `)` then `B.R` data), `C.R`.", ["A.R", "C.R"]),
            (
                "`P_1998_p.R` (effect on Ka: `ka <- exp(lka + e * X)` (29% lower; Table 4)."
                " The CL effect (oral only (paper p. 3)......................",
                ["P_1998_p.R"],
            ),
        ],
        ids=[
            "nagy",
            "interval-then-stops",
            "unclosed",
            "two-intervals",
            "mention",
            "paren-in-code",
            "never-closed",
        ],
    )
    def test_an_open_paren_hides_no_entry(self, body: str, models: list[str]) -> None:
        """A ``(`` nothing closes, and either bracket of a half-open interval,
        is text: the entries after it are entries, so a carried entry is not
        carried again by the next dedup."""
        dd = load_helper("dedup_canonical_headers")
        assert [e.model for e in dd._split_entries(body)[1]] == models

    def test_text_is_kept_only_as_whole_words(self, tmp_path: Path) -> None:
        """ "No" is not kept by "Nothing", nor "word" by "worde.g.": a dropped
        copy's prose and notes compare on word boundaries."""
        dd = load_helper("dedup_canonical_headers")
        assert (dd._within("No", "Nothing more"), dd._within("word", "worde.g. x")) == (
            False,
            False,
        )
        assert dd._within("a  b", "x a b, y") is True
        kept = "- **Example models:** `A_2020_a.R` (a). Nothing more is known.\n"
        dropped = "- **Example models:** `A_2020_a.R` (a). No.\n"
        proc, _, report = self._dedup(tmp_path, _pair(kept, dropped))
        assert proc.returncode == 0
        assert "In a dropped copy:\n\n````\nNo\n````\n" in report

    @pytest.mark.parametrize(
        ("kept", "dropped", "reported"),
        [
            (
                "- **Example models:** `A_2020_a.R` (CrCL capped at 1200).\n",
                "- **Example models:** `A_2020_a.R` (CrCL capped at 120).\n",
                "`A_2020_a.R` (CrCL capped at 120)",
            ),
            (
                "- **Source aliases:**\n  Weights capped at 1200 kg.\n",
                "- **Source aliases:**\n  Weights capped at 120\n",
                "  Weights capped at 120",
            ),
        ],
        ids=["entry-note", "source-alias-text"],
    )
    def test_a_note_or_alias_text_is_kept_only_as_whole_words(
        self, tmp_path: Path, kept: str, dropped: str, reported: str
    ) -> None:
        """ "120" is not kept by "1200": a dropped copy's entry notes and
        source-alias text compare on word boundaries, as its prose does."""
        proc, _, report = self._dedup(tmp_path, _pair(kept, dropped))
        assert proc.returncode == 0
        assert f"In a dropped copy:\n\n````\n{reported}\n````\n" in report

    @pytest.mark.parametrize(
        ("kept", "result"),
        [
            (f"{NAGY}.............", f"{NAGY}, `B_2021_b.R` (b)............."),
            ("`A_2020_a.R` (unclosed note.", "`A_2020_a.R` (unclosed note, `B_2021_b.R` (b)."),
            (
                "`P_1998_p.R` (CL effect (oral only (paper p. 3)......",
                "`P_1998_p.R` (CL effect (oral only (paper p. 3), `B_2021_b.R` (b)......",
            ),
        ],
        ids=["nagy", "unclosed", "never-closed"],
    )
    def test_dedup_twice_is_dedup_once(self, tmp_path: Path, kept: str, result: str) -> None:
        """The entry carried after an open paren is an entry: a second copy
        listing it again carries nothing."""
        dropped = "- **Example models:** `B_2021_b.R` (b).\n"
        proc, once, _ = self._dedup(tmp_path, _pair(f"- **Example models:** {kept}\n", dropped))
        assert (proc.returncode, proc.stderr) == (0, "")
        assert once == f"### Q (**canonical for q**)\n- **Example models:** {result}\n{KEPT_NOTES}"
        again = once + "\n### Q (**canonical for q**)\n" + dropped
        proc, twice, report = self._dedup(tmp_path, again)
        assert (proc.returncode, proc.stderr, report, twice) == (0, "", "", once)
        assert proc.stdout.endswith("  Q: 2 -> 1\n")

    def test_the_copy_on_the_base_is_kept(self, tmp_path: Path) -> None:
        """With --prefer, the base's wording stays even when a branch's copy is
        longer; the branch's own words go to the report, so the merge reverts
        nothing of the base's."""
        base_copy = (
            "### Q (**canonical for q**)\n- **Notes:** As main words it.\n"
            "- **Example models:** `A_2020_a.R` (a).\n"
        )
        branch_copy = (
            "### Q (**canonical for q**)\n- **Notes:** As the branch words it, at more length.\n"
            "- **Example models:** `B_2021_b.R` (b).\n"
        )
        base = tmp_path / "base.md"
        base.write_text(base_copy)
        register = tmp_path / "covariate-columns.md"
        report = tmp_path / "hand-union.md"
        register.write_text(branch_copy + "\n" + base_copy)
        proc = run_helper(
            "dedup_canonical_headers.py", "--global", "--report", report, "--prefer", base, register
        )
        assert proc.returncode == 0, proc.stderr
        assert register.read_text() == base_copy.replace("(a).", "(a), `B_2021_b.R` (b).")
        assert "- **Notes:** As the branch words it, at more length.\n" in report.read_text()
        missing = tmp_path / "no-base.md"
        proc = run_helper("dedup_canonical_headers.py", "--prefer", missing, register)
        assert (proc.returncode, proc.stderr) == (2, f"ERROR: (dedup) no such file: {missing}\n")

    @pytest.mark.parametrize("fixture", sorted(HISTORY.glob("*.md")), ids=lambda p: p.stem)
    def test_real_duplicates_lose_no_model_and_no_word(self, fixture: Path, tmp_path: Path) -> None:
        """Every duplicate group a consolidation of nlmixr2lib met, as the
        register had it before the dedup ran (the commit is in the file name).

        Read without the dedup's parser: every word of every copy, and every
        backticked ``*.R`` of a copy's Example models, is in the kept block
        or the report. With it: every model a copy lists as an entry is an
        entry of the kept block. --check is clean.
        """
        dd = load_helper("dedup_canonical_headers")
        before = fixture.read_text()
        scope = () if "compartment-names" in fixture.name else ("--global",)
        proc, after, report = self._dedup(tmp_path, before, *scope)
        register = tmp_path / "covariate-columns.md"
        assert proc.returncode == 0, proc.stderr
        assert "merge dedup" not in after
        check = run_helper("dedup_canonical_headers.py", *scope, "--check", register)
        assert check.returncode == 0, check.stderr

        kept = _plain_blocks(after)
        for name, copies in _plain_blocks(before).items():
            assert (len(copies), len(kept[name])) == (2, 1), name
            result = kept[name][0]
            lost = {w for c in copies for w in _words(c)} - _words(result) - _words(report)
            assert lost - UNCOUNTED == set(), name
            kept_text = _example_text(result) + report
            for copy in copies:
                models = set(re.findall(r"`([A-Za-z0-9_.-]+\.R)`", _example_text(copy)))
                assert {m for m in models if f"`{m}`" not in kept_text} == set()
            listed = {e.model for c in _blocks(dd, before)[name] for e in _entries(dd, c)}
            assert listed <= {e.model for e in _entries(dd, _blocks(dd, after)[name][0])}

    @pytest.mark.parametrize(
        ("fixture", "name"),
        [
            (path, name)
            for path in sorted(SHAPES.glob("*.md"))
            for name in _plain_blocks(path.read_text())
        ],
        ids=lambda v: v.stem if isinstance(v, Path) else v,
    )
    def test_a_real_shape_dropped_loses_no_model_and_no_word(
        self, fixture: Path, name: str, tmp_path: Path
    ) -> None:
        """The 24 blocks of the 2026-10-23 nlmixr2lib registers with an
        Example-models field that has sub-bullets under an inline list, is
        wrapped over lines, or has a "(" that never closes: each is the dropped
        copy beside a longer one that lists a single other model."""
        dd = load_helper("dedup_canonical_headers")
        block = _plain_blocks(fixture.read_text())[name][0].rstrip("\n")
        keeper, skipping = [], False
        for line in block.split("\n"):
            if FIELD_LINE.match(line):
                skipping = line.startswith("- **Example models:**")
                if skipping:
                    line = "- **Example models:** `Zkeeper_2099_z.R` (the keeper's only model)."
                    keeper.append(line)
                    continue
            if not skipping:
                keeper.append(line)
        keeper.append("- **Notes:** " + "padding " * 400)
        scope = () if "compartment-names" in fixture.name else ("--global",)
        text = "## S\n\n" + "\n".join(keeper) + "\n\n" + block + "\n"
        proc, after, report = self._dedup(tmp_path, text, *scope)
        assert proc.returncode == 0, proc.stderr
        (result,) = _plain_blocks(after)[name]
        assert _words(block) - _words(result) - _words(report) - UNCOUNTED == set()
        models = set(re.findall(r"`([A-Za-z0-9_.-]+\.R)`", _example_text(block)))
        kept_text = _example_text(result) + report
        assert {m for m in models if f"`{m}`" not in kept_text} == set()
        dropped = _blocks(dd, "## S\n\n" + block)[name][0]
        kept = _blocks(dd, after)[name][0]
        assert {e.model for e in _entries(dd, dropped)} <= {e.model for e in _entries(dd, kept)}


class TestVerifyVignettesParallel:
    """Nothing here renders a vignette: that needs nlmixr2lib and rxode2."""

    @pytest.fixture
    def rscript(self) -> str:
        rscript = shutil.which("Rscript")
        if rscript is None:
            pytest.skip("Rscript is not on PATH (CI installs no R), so the R script is not run")
        return rscript

    @pytest.fixture
    def rscript_with_callr(self, rscript: str) -> str:
        """Rscript, when it can load callr: the script loads callr before anything else."""
        probe = run(
            rscript,
            "-e",
            'quit(status = if (requireNamespace("callr", quietly = TRUE)) 0L else 1L)',
        )
        if probe.returncode != 0:
            pytest.skip("the R package callr is not installed, so the R script cannot start")
        return rscript

    def test_parses(self, rscript: str) -> None:
        proc = run(rscript, "-e", "invisible(parse(file = commandArgs(TRUE)[1]))", VIGNETTES_R)
        assert (proc.returncode, proc.stderr) == (0, "")

    def test_no_vignettes_is_a_clean_pass(self, rscript_with_callr: str, tmp_path: Path) -> None:
        """--skip-install keeps it off the network; no results file is written."""
        results = tmp_path / "results.jsonl"
        proc = run(
            rscript_with_callr,
            VIGNETTES_R,
            "--worktree",
            tmp_path,
            "--skip-install",
            "--results",
            results,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == (
            "--- no DESCRIPTION found; falling back to the full library path ---\n"
            "no vignettes found under vignettes/articles/; nothing to validate\n"
        )
        assert not results.exists()

    def test_only_rejects_names_it_cannot_find(
        self, rscript_with_callr: str, tmp_path: Path
    ) -> None:
        """A typo in --only must not look like a pass."""
        proc = run(
            rscript_with_callr,
            VIGNETTES_R,
            "--worktree",
            tmp_path,
            "--skip-install",
            "--results",
            tmp_path / "results.jsonl",
            "--only",
            "nope,Other.Rmd",
        )
        assert proc.returncode == 2, proc.stderr
        assert proc.stdout.endswith(
            "--only names 2 vignette(s) not present under vignettes/articles: nope, Other\n"
        )
