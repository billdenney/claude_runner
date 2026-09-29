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

restore_dropped_sections.py and union_merge_news.py have their own modules for
their logic; here they get their CLI, fail-loud and ``--extra-ref`` cases.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

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
    "    (placement) OK — every (canonical, model) pair a branch recorded is still filed"
    f" under that canonical in {REGISTER}\n"
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
            "  8  parallel vignette validation failed, or the validator could not run\n"
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


# The helpers that read the consolidation worktree, and the prefix of their errors.
WORKTREE_HELPERS = {
    "verify_section_headers.py": "section-verifier",
    "verify_register_placement.py": "placement",
    "union_merge_lines.py": "union-merge",
    "restore_dropped_sections.py": "restore",
    "union_merge_news.py": "news",
}
# restore and news find the worktree with `git worktree list`; the others at
# <repo>/.worktrees/<branch>.
_LISTED = {"restore_dropped_sections.py", "union_merge_news.py"}
_VERIFIERS = {"verify_section_headers.py", "verify_register_placement.py"}


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
        ("body", "entries", "tail"),
        [
            (
                f"{NAGY}.............",
                [("Nagy_2017_x.R", NAGY.split(" ", 1)[1])],
                ".............",
            ),
            (
                "`P_1998_p.R` (effect on Ka: `ka <- exp(lka + e * X)` (29% lower; Table 4)."
                " The CL effect (oral only (paper p. 3)......................",
                [
                    (
                        "P_1998_p.R",
                        "(effect on Ka: `ka <- exp(lka + e * X)` (29% lower; Table 4). The CL"
                        " effect (oral only (paper p. 3)",
                    )
                ],
                "......................",
            ),
            (
                "`A.R` (a), `B.R`, `C.R` (c (nested) note). Prose after the list.",
                [("A.R", "(a)"), ("B.R", ""), ("C.R", "(c (nested) note)")],
                ". Prose after the list.",
            ),
            ("`A.R` (a)", [("A.R", "(a)")], ""),
        ],
        ids=["unclosed-then-stops", "unclosed-never-closed", "prose-after", "no-final-stop"],
    )
    def test_the_entries_and_what_follows_them_rebuild_the_body(
        self, body: str, entries: list[tuple[str, str]], tail: str
    ) -> None:
        """The text after the last entry is kept, not replaced by one full stop."""
        assert self.u.split_example_body(body) == (entries, tail)
        rebuilt = ", ".join(f"`{f}` {a}" if a else f"`{f}`" for f, a in entries) + tail
        assert rebuilt == body

    def test_an_unclosed_annotation_stops_before_the_final_stops_and_the_next_entry(
        self,
    ) -> None:
        assert self.u.split_example_body(f"{NAGY}.............") == (
            [("Nagy_2017_x.R", NAGY.split(" ", 1)[1])],
            ".............",
        )
        assert self.u.split_example_body("`A.R` (a (b] c)..., `B.R` (b).") == (
            [("A.R", "(a (b] c)..."), ("B.R", "(b)")],
            ".",
        )

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

- **Example models:** `B_2021_col.R` (with a longer annotation), `A_2020_col.R` (merged from a duplicate register entry during merge dedup).

## Metabolites

### col (**suffix for a colistin metabolite**)

- **Example models:** `C_2022_col.R`.
"""

    def test_collapses_a_duplicate_within_a_section(self, tmp_path: Path) -> None:
        """The longest block is kept and gains the other's example model.

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
