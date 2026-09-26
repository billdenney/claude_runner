"""Run every runner-merge-claude-branches helper script at least once.

The skill's scripts ship in the wheel, and Claude Code agents run them through
bash, python3 or Rscript; merge_branches.sh also runs
verify_branch_contributions.sh itself. None of them is imported by the
package, so a script that no test runs can break without anything going red.
Each gets a syntax or ``--help`` run here, plus a cheap known-answer case:

* ``merge_branches.sh --dry-run`` surveys the task branches and stops before it
  creates a worktree, so it is the one invocation beyond ``--help`` with no
  side effects. The steps after the survey (merging, R regeneration,
  ``devtools::check``, vignettes, the push) need an R package and a remote to
  push to, so they are not run; the helpers they call are run one by one below.
* The verifiers and repair scripts run against the loss they exist for: two
  task branches each add a model to the same Example-models line and append a
  canonical block at the same place, and the second ``-X theirs`` merge keeps
  only the second branch's side of both. The first branch's model and its
  whole ``### HT`` block vanish.
* ``verify_vignettes_parallel.R`` needs R, which CI does not install. It is
  parse-checked, and run against a worktree with no vignettes, only where
  Rscript is on PATH.

restore_dropped_sections.py and union_merge_news.py have their own modules for
their logic. Here they get the ``--help`` run, and restore_dropped_sections.py
closes the loss in the pipeline test.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from claude_task_runner.cli.install_skills_cmd import _packaged_skill_dir

from ._git_world import git, isolate_git

SKILL_DIR = _packaged_skill_dir("runner-merge-claude-branches")
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


def _run(*cmd: str | Path, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run a script with stdin closed, so a prompt can never wait on a terminal."""
    return subprocess.run(
        [str(c) for c in cmd],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _python(script: str, *args: str | Path) -> subprocess.CompletedProcess[str]:
    return _run(sys.executable, SKILL_DIR / script, *args)


def _commit(repo: Path, files: dict[str, str], message: str) -> None:
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(repo, "add", *files)
    git(repo, "commit", "-qm", message)


def _push_task_branch(repo: Path, branch: str, files: dict[str, str], message: str) -> None:
    """Commit ``files`` on ``branch``, cut from main, and push it as a task worker does."""
    git(repo, "checkout", "-q", "-b", branch, "main")
    _commit(repo, files, message)
    git(repo, "push", "-q", "origin", branch)
    git(repo, "checkout", "-q", "main")


def _survey_rows(stdout: str) -> list[tuple[str, str, str, str]]:
    """(branch, ahead, files, subject) for each branch merge_branches.sh surveyed."""
    return re.findall(r"^ {6}(\S+) +ahead=(\d+)  files=(\d+)  (.*)$", stdout, re.MULTILINE)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A clone whose origin carries main and three ``claude/*`` task branches.

    ``claude/merged`` is already folded into main, so a survey must leave it
    out. ``claude/a`` and ``claude/b`` fork from that main, so each is one
    commit ahead and changes exactly the two files it pushed.
    """
    isolate_git(tmp_path, monkeypatch)
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    repo = tmp_path / "repo"
    git(tmp_path, "clone", "-q", str(origin), str(repo))
    _commit(repo, {".gitignore": ".worktrees/\n", REGISTER: BASE_REGISTER}, "base")
    git(repo, "checkout", "-q", "-b", "claude/merged")
    _commit(repo, {"inst/modeldb/M_2019_m.R": "# M\n"}, "Add M 2019 model")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "-q", "--no-ff", "--no-edit", "claude/merged")
    git(repo, "push", "-q", "origin", "main", "claude/merged")
    _push_task_branch(
        repo,
        "claude/a",
        {REGISTER: REGISTER_A, "inst/modeldb/A_2020_a.R": "# A\n"},
        "Add A 2020 model",
    )
    _push_task_branch(
        repo,
        "claude/b",
        {REGISTER: REGISTER_B, "inst/modeldb/B_2021_b.R": "# B\n"},
        "Add B 2021 model",
    )
    return repo


@pytest.fixture
def worktree(repo: Path) -> Path:
    """The consolidation worktree after folding in claude/a, then claude/b.

    This is merge_branches.sh's step 3: ``git merge --no-ff -X theirs`` per
    branch, in a worktree at ``<repo>/.worktrees/<branch>``, which is where
    every helper below looks for the merged file.
    """
    git(repo, "fetch", "-q", "origin")
    wt = repo / ".worktrees" / CONSOLIDATION
    git(repo, "worktree", "add", "-q", "-b", CONSOLIDATION, str(wt), "origin/main")
    for branch in ("origin/claude/a", "origin/claude/b"):
        git(wt, "merge", "-q", "--no-ff", "--no-edit", "-X", "theirs", branch)
    assert (wt / REGISTER).read_text() == REGISTER_B, "the fixture did not reproduce the loss"
    return wt


def _verifier_args(repo: Path) -> list[str | Path]:
    return ["--repo", repo, "--branch", CONSOLIDATION, "--file", REGISTER]


class TestMergeBranches:
    def test_parses(self) -> None:
        proc = _run("bash", "-n", MERGE_BRANCHES)
        assert (proc.returncode, proc.stderr) == (0, "")

    def test_help(self) -> None:
        proc = _run("bash", MERGE_BRANCHES, "--help")
        assert (proc.returncode, proc.stderr) == (0, "")
        assert proc.stdout.startswith(
            "Consolidate per-task claude/* branches into one review-ready branch.\n"
        )
        assert "\nUsage:\n  merge_branches.sh [OPTIONS]\n" in proc.stdout

    def test_unknown_argument_exits_2(self) -> None:
        proc = _run("bash", MERGE_BRANCHES, "--no-such-flag")
        assert (proc.returncode, proc.stderr) == (2, "unknown arg: --no-such-flag\n")

    def test_dry_run_surveys_the_unmerged_branches_and_stops(self, repo: Path) -> None:
        proc = _run("bash", MERGE_BRANCHES, "--repo", repo, "--dry-run")
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

    def test_dry_run_aborts_when_two_branches_add_one_path_differently(self, repo: Path) -> None:
        """-X theirs would keep only the last one merged, so the survey stops.

        claude/c and claude/e add the same bytes, which is not a collision on
        its own; claude/d adds different ones. Excluding claude/d, the remedy
        the error names, lets the survey through.
        """
        model = "inst/modeldb/Wang_2019_tacrolimus.R"
        added = {"c": "# paper one\n", "d": "# paper two\n", "e": "# paper one\n"}
        for branch, text in added.items():
            _push_task_branch(repo, f"claude/{branch}", {model: text}, f"Add Wang 2019 ({branch})")

        proc = _run("bash", MERGE_BRANCHES, "--repo", repo, "--dry-run")
        assert proc.returncode == 4, proc.stdout
        assert proc.stderr == (
            "ERROR: the same new path is added with different content by more than one branch:\n"
            f"    {model}  <-  origin/claude/c origin/claude/d origin/claude/e\n"
            "    -X theirs would keep only the last one merged. Reletter one branch's files or"
            " --exclude-ref one of them, then re-run.\n"
        )

        proc = _run(
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


class TestVerifyBranchContributions:
    """merge_branches.sh runs this one directly, so these tests do too."""

    def test_parses(self) -> None:
        proc = _run("bash", "-n", VERIFY_CONTRIBUTIONS)
        assert (proc.returncode, proc.stderr) == (0, "")

    def test_help(self) -> None:
        proc = _run(VERIFY_CONTRIBUTIONS, "--help")
        assert (proc.returncode, proc.stderr) == (0, "")
        assert proc.stdout.startswith(
            "Verify no per-branch model contributions were lost from a structured-\n"
        )
        assert "\nUsage:\n  verify_branch_contributions.sh [OPTIONS]\n" in proc.stdout

    def test_branch_is_required(self) -> None:
        proc = _run(VERIFY_CONTRIBUTIONS)
        assert (proc.returncode, proc.stderr) == (2, "ERROR: --branch is required\n")

    def test_unknown_argument_exits_2(self) -> None:
        proc = _run(VERIFY_CONTRIBUTIONS, "--no-such-flag")
        assert (proc.returncode, proc.stderr) == (2, "unknown arg: --no-such-flag\n")

    def test_empty_file_disables_it(self) -> None:
        proc = _run(VERIFY_CONTRIBUTIONS, "--branch", CONSOLIDATION, "--file", "")
        assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")

    def test_reports_the_model_and_block_the_merge_dropped(
        self, repo: Path, worktree: Path
    ) -> None:
        """All three checks fail on the post-merge file, in one report."""
        proc = _run(VERIFY_CONTRIBUTIONS, *_verifier_args(repo))
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


@pytest.mark.parametrize(
    "script",
    [
        "dedup_canonical_headers.py",
        "restore_dropped_sections.py",
        "union_merge_lines.py",
        "union_merge_news.py",
        "verify_register_placement.py",
        "verify_section_headers.py",
    ],
)
def test_python_helper_help(script: str) -> None:
    proc = _python(script, "--help")
    assert (proc.returncode, proc.stderr) == (0, "")
    assert proc.stdout.startswith(f"usage: {script} [-h]")


def test_section_headers_reports_the_dropped_block(repo: Path, worktree: Path) -> None:
    proc = _python("verify_section_headers.py", *_verifier_args(repo))
    assert (proc.returncode, proc.stdout, proc.stderr) == (1, SECTION_REPORT, "")


class TestRegisterPlacement:
    def test_reports_each_pair_the_merge_dropped(self, repo: Path, worktree: Path) -> None:
        """The WT pair is lost even though the WT block itself survived."""
        proc = _python("verify_register_placement.py", *_verifier_args(repo))
        assert (proc.returncode, proc.stdout, proc.stderr) == (1, PLACEMENT_REPORT, "")

    def test_unresolvable_branch_is_an_error_not_a_pass(self, repo: Path) -> None:
        """Every ancestor check would fail against it, reporting a clean register."""
        merged = repo / ".worktrees" / "no-such-branch" / REGISTER
        merged.parent.mkdir(parents=True)
        merged.write_text(BASE_REGISTER)
        proc = _python(
            "verify_register_placement.py",
            "--repo",
            repo,
            "--branch",
            "no-such-branch",
            "--file",
            REGISTER,
        )
        assert (proc.returncode, proc.stdout) == (2, "")
        assert proc.stderr == (
            "ERROR: (placement) --branch 'no-such-branch' does not resolve to a commit in"
            f" {repo}; refusing to report a vacuous pass.\n"
        )


def test_union_merge_lines_restores_the_dropped_example_model(repo: Path, worktree: Path) -> None:
    """Merged order first, then each branch's new models, keeping each annotation."""
    merged = worktree / REGISTER
    proc = _python("union_merge_lines.py", *_verifier_args(repo))
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
    assert _python("union_merge_lines.py", *_verifier_args(repo)).returncode == 0
    proc = _python("restore_dropped_sections.py", *_verifier_args(repo))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (worktree / REGISTER).read_text() == (
        _with_models(B_ENTRY, A_ENTRY) + BMI_BLOCK + HT_BLOCK
    )

    proc = _run(VERIFY_CONTRIBUTIONS, *_verifier_args(repo))
    assert (proc.returncode, proc.stderr) == (0, "")
    assert proc.stdout == (
        "    (placement) OK — every (canonical, model) pair a branch recorded is still filed"
        f" under that canonical in {REGISTER}\n"
        "    (verifier) OK — all per-branch *.R additions and brand-new ##/### canonical-section"
        f" headers are present in {REGISTER}\n"
    )


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

        proc = _python("dedup_canonical_headers.py", "--check", register)
        assert (proc.returncode, proc.stdout) == (1, "")
        assert proc.stderr == (
            f"# DUPLICATE canonical headers (per-##-section) in {register}: 1\n"
            "#   [Drugs] col x2 (lines [5, 9])\n"
        )
        assert register.read_text() == self.DUPLICATED, "--check must not edit"

        proc = _python("dedup_canonical_headers.py", register)
        assert (proc.returncode, proc.stderr) == (0, "")
        assert proc.stdout == (
            f"deduped 1 canonical name(s) in {register}:\n"
            "  col: 2 -> 1  (+examples ['A_2020_col.R'])\n"
        )
        assert register.read_text() == self.DEDUPED

        proc = _python("dedup_canonical_headers.py", "--check", register)
        assert (proc.returncode, proc.stdout) == (0, "")
        assert proc.stderr == f"# clean (no per-##-section duplicate canonicals): {register}\n"

    def test_global_scope_flags_reuse_across_sections(self, tmp_path: Path) -> None:
        """The covariate register's scope: one canonical, one block, file-wide."""
        register = tmp_path / "covariate-columns.md"
        register.write_text(self.DEDUPED)
        proc = _python("dedup_canonical_headers.py", "--global", "--check", register)
        assert (proc.returncode, proc.stdout) == (1, "")
        assert proc.stderr == (
            f"# DUPLICATE canonical headers (whole-file) in {register}: 1\n"
            "#   col x2 (lines [5, 11])\n"
        )


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
        probe = _run(
            rscript,
            "-e",
            'quit(status = if (requireNamespace("callr", quietly = TRUE)) 0L else 1L)',
        )
        if probe.returncode != 0:
            pytest.skip("the R package callr is not installed, so the R script cannot start")
        return rscript

    def test_parses(self, rscript: str) -> None:
        proc = _run(rscript, "-e", "invisible(parse(file = commandArgs(TRUE)[1]))", VIGNETTES_R)
        assert (proc.returncode, proc.stderr) == (0, "")

    def test_no_vignettes_is_a_clean_pass(self, rscript_with_callr: str, tmp_path: Path) -> None:
        """--skip-install keeps it off the network; no results file is written."""
        results = tmp_path / "results.jsonl"
        proc = _run(
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
        proc = _run(
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
