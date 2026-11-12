"""merge_branches.sh --fold-in: a later round into an existing consolidation.

A branch pushed after a consolidation forks from main, not from the
consolidation, so ``-X theirs`` resolves its NEWS.md hunk to main's text plus
its own bullet: the development-version bullets the consolidation added
vanish, and so do the Example-models entries it added to a line the late
branch also edited. A fold-in round runs every repair and gate again with the
consolidation's tip as the base, so the consolidation's own copies are the
history the late branches' additions are applied to.

Every case runs merge_branches.sh as an operator does, without the R steps.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

import pytest

from ._git_world import git
from ._merge_skill_repo import SKILL_DIR, new_repo, push_task_branch, run

MERGE_BRANCHES = SKILL_DIR / "merge_branches.sh"
REGISTER = "inst/references/covariate-columns.md"
BRANCH = "consolidation"
NEWS = "# development version\n\n- Add Base 2000 x model.\n"
BASE_REGISTER = """\
# Covariate columns

## Body size

### WT (**canonical for body weight**)

- **Notes:** Base note.
- **Example models:** `Base_2000_x.R` (base).
"""
NO_R = ("--skip-r-regen", "--skip-check", "--skip-vignettes")


def push_model_branch(
    repo: Path, name: str, notes: str | None = None, register: str = BASE_REGISTER
) -> None:
    """claude/<name>, cut from main, whose register is ``register``: a model,
    its NEWS bullet, its WT entry and, with ``notes``, a sentence appended to
    WT's Notes line."""
    upper = name.upper()
    model = f"{upper}_2020_{name}.R"
    register = register.replace("(base).", f"(base), `{model}` ({name}).")
    if notes:
        register = register.replace("Base note.", f"Base note. {notes}")
    files = {
        "NEWS.md": NEWS.replace("\n\n", f"\n\n- Add {upper} 2020 {name} model.\n", 1),
        REGISTER: register,
        f"inst/modeldb/{model}": f"# {name}\n",
    }
    push_task_branch(repo, f"claude/{name}", files, f"Add {upper} 2020 {name} model")


def merge(repo: Path, *flags: str) -> subprocess.CompletedProcess[str]:
    return run("bash", MERGE_BRANCHES, "--repo", repo, "--branch-name", BRANCH, *flags)


def path_in_git_dir(wt: Path, name: str) -> Path:
    return Path(git(wt, "rev-parse", "--path-format=absolute", "--git-path", name).strip())


@pytest.fixture
def consolidated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A first round that merged claude/a and claude/b and pushed the branch;
    then claude/c, cut from main, is pushed."""
    repo = new_repo(tmp_path, monkeypatch, {"NEWS.md": NEWS, REGISTER: BASE_REGISTER})
    push_model_branch(repo, "a")
    push_model_branch(repo, "b")
    first = merge(repo, "--yes", *NO_R)
    assert first.returncode == 0, first.stdout + first.stderr
    push_model_branch(repo, "c")
    return repo


def test_a_fold_in_keeps_every_bullet_and_entry_the_consolidation_added(
    consolidated: Path,
) -> None:
    repo = consolidated
    wt = repo / ".worktrees" / BRANCH
    tip = git(wt, "rev-parse", "HEAD").strip()
    news_before = (wt / "NEWS.md").read_text()
    assert "- Add A 2020 a model." in news_before and "- Add B 2020 b model." in news_before
    proc = merge(repo, "--fold-in", "--yes", *NO_R)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (
        "==> Surveying origin/claude/* branches with unmerged commits vs consolidation (fold-in)\n"
        in proc.stdout
    )
    assert "    found 1 unmerged branch(es):\n" in proc.stdout
    # The merge itself lost the consolidation's bullets and claude/b's entry...
    merged = git(wt, "log", "--first-parent", "--format=%H", "-1", "--grep", "claude/c").strip()
    news_merged = git(wt, "show", f"{merged}:NEWS.md")
    assert news_merged == NEWS.replace("\n\n", "\n\n- Add C 2020 c model.\n", 1).rstrip("\n")
    assert "B_2020_b.R" not in git(wt, "show", f"{merged}:{REGISTER}")
    # ...and the round's repairs put every one back, on top of the consolidation's copy.
    assert (wt / "NEWS.md").read_text() == news_before.replace(
        "# development version\n", "# development version\n\n- Add C 2020 c model.\n", 1
    )
    assert (wt / REGISTER).read_text() == BASE_REGISTER.replace(
        "(base).", "(base), `C_2020_c.R` (c), `B_2020_b.R` (b), `A_2020_a.R` (a)."
    )
    inputs = path_in_git_dir(wt, "merge-inputs").read_text()
    assert f"base\t{tip}\npr-base\torigin/main\n" in inputs
    assert [line for line in inputs.splitlines() if line.startswith("merged")] == [
        f"merged\torigin/claude/c\t{git(repo, 'rev-parse', 'origin/claude/c').strip()}"
    ]
    assert path_in_git_dir(wt, f"merge-inputs.before-{tip[:12]}").is_file()
    assert "\nMerge 3 claude/* branches (3 new models)\n" in proc.stdout
    assert "This round folded 1 branch(es) into consolidation; the PR text above\n" in proc.stdout
    assert (
        git(repo, "ls-remote", "origin", BRANCH).split()[0] == git(wt, "rev-parse", "HEAD").strip()
    )


def test_a_fold_in_with_nothing_new_does_nothing(consolidated: Path) -> None:
    repo = consolidated
    git(repo, "push", "-q", "origin", "--delete", "claude/c")
    proc = merge(repo, "--fold-in", "--yes", *NO_R)
    assert (proc.returncode, proc.stderr) == (0, "")
    assert proc.stdout.endswith("==> No unmerged branches found. Nothing to do.\n")


def test_a_late_branch_that_overwrites_a_line_of_the_consolidation_stops_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """claude/a's sentence is on the consolidation; claude/c's -X theirs hunk
    replaces the Notes line with main's plus its own. Against the
    consolidation's tip that is reverted content, which stops the round."""
    repo = new_repo(tmp_path, monkeypatch, {"NEWS.md": NEWS, REGISTER: BASE_REGISTER})
    push_model_branch(repo, "a", notes="Alpha sentence.")
    first = merge(repo, "--yes", *NO_R)
    assert first.returncode == 0, first.stdout + first.stderr
    push_model_branch(repo, "c", notes="Gamma sentence.")
    proc = merge(repo, "--fold-in", "--yes", *NO_R)
    assert proc.returncode == 5, proc.stdout + proc.stderr
    assert "      missing line: - **Notes:** Base note. Alpha sentence.\n" in proc.stdout
    assert "ERROR: the merge reverted register content " in proc.stderr


def test_a_path_the_consolidation_has_with_other_content_stops_the_survey(
    consolidated: Path,
) -> None:
    repo = consolidated
    push_task_branch(repo, "claude/d", {"inst/modeldb/A_2020_a.R": "# another A\n"}, "Add A again")
    proc = merge(repo, "--fold-in", "--yes", *NO_R)
    assert proc.returncode == 4, proc.stdout + proc.stderr
    assert proc.stderr == (
        "ERROR: origin/claude/d adds inst/modeldb/A_2020_a.R, which consolidation already has"
        " with other content.\n"
        "    -X theirs would keep only the last one merged. Reletter one branch's files or"
        " --exclude-ref one of them, then re-run.\n"
    )


@pytest.mark.parametrize(
    ("given", "code", "message"),
    [
        (
            ["--base", "origin/claude/a"],
            2,
            "--fold-in counts the branch from the run's --base 'origin/main'; --base"
            " 'origin/claude/a' conflicts with it.",
        ),
        (
            ["--pattern", "origin/other/*"],
            2,
            "--fold-in uses the run's --pattern 'origin/claude/*'; --pattern 'origin/other/*'"
            " conflicts with it.",
        ),
        (["--branch-name", "elsewhere"], 3, None),
    ],
    ids=["base", "pattern", "no-worktree"],
)
def test_a_fold_in_refuses_inputs_that_cannot_be_right(
    consolidated: Path, given: list[str], code: int, message: str | None
) -> None:
    proc = run(
        "bash",
        MERGE_BRANCHES,
        "--repo",
        consolidated,
        "--branch-name",
        BRANCH,
        "--fold-in",
        "--yes",
        *NO_R,
        *given,
    )
    expected = message or (
        f"--fold-in: no worktree at {consolidated}/.worktrees/elsewhere; a fold-in adds branches"
        " to the consolidation a run made there."
    )
    assert (proc.returncode, proc.stderr) == (code, f"ERROR: {expected}\n")


def test_a_fold_in_refuses_a_dirty_worktree(consolidated: Path) -> None:
    wt = consolidated / ".worktrees" / BRANCH
    (wt / "stray.txt").write_text("x\n")
    proc = merge(consolidated, "--fold-in", "--yes", *NO_R)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert proc.stderr.startswith(
        f"ERROR: --fold-in: {wt} has uncommitted changes:\n    ?? stray.txt\n"
    )


FOO_A = "\n### FOO (**canonical for foo**)\n\n- **Notes:** Alpha note.\n- **Example models:** `Fa_2020_a.R` (a).\n"
FOO_B = "\n### FOO (**canonical for foo**)\n\n- **Notes:** Beta note, the longer one.\n- **Example models:** `Fb_2021_b.R` (b).\n"


@pytest.fixture
def stopped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A first round that stopped for a hand union: claude/fa and claude/fb
    register FOO in different words. Then claude/c is pushed."""
    age = "\n## Age\n\n### AGE (**canonical for age**)\n\n- **Example models:** `Age_2000_x.R` (age).\n"
    repo = new_repo(tmp_path, monkeypatch, {"NEWS.md": NEWS, REGISTER: BASE_REGISTER + age})
    push_task_branch(repo, "claude/fa", {REGISTER: BASE_REGISTER + FOO_A + age}, "Add Fa")
    push_task_branch(repo, "claude/fb", {REGISTER: BASE_REGISTER + age + FOO_B}, "Add Fb")
    first = merge(repo, "--yes", *NO_R)
    assert first.returncode == 9, first.stdout + first.stderr
    push_model_branch(repo, "c", register=BASE_REGISTER + age)
    return repo


def test_a_report_the_round_before_left_stops_a_fold_in(stopped: Path) -> None:
    """Until --hand-union-done says the report was worked through; it is then
    kept beside the new round's, which starts empty."""
    wt = stopped / ".worktrees" / BRANCH
    report = path_in_git_dir(wt, "hand-union-report.md")
    held = report.read_text()
    proc = merge(stopped, "--fold-in", "--yes", *NO_R)
    assert proc.returncode == 9, proc.stdout + proc.stderr
    assert proc.stderr.startswith(held)
    assert proc.stderr.endswith(
        "\nSTOP: the round before left the hand-union report above, and no\n"
        "  --finish --hand-union-done confirmed it:\n"
        f"    {report}\n"
        f"  Work it through in {wt} and commit (or finish that round first), then run\n"
        "  --fold-in again with --hand-union-done.\n"
    )
    assert "Merge branch 'claude/c'" not in git(wt, "log", "--format=%s")
    tip = git(wt, "rev-parse", "HEAD").strip()
    done = merge(stopped, "--fold-in", "--hand-union-done", "--yes", *NO_R)
    assert done.returncode == 0, done.stdout + done.stderr
    assert path_in_git_dir(wt, f"hand-union-report.before-{tip[:12]}.md").read_text() == held
    assert not report.exists()


def test_a_report_a_finish_confirmed_does_not_stop_a_fold_in(stopped: Path) -> None:
    wt = stopped / ".worktrees" / BRANCH
    finish = merge(stopped, "--finish", "--hand-union-done")
    assert finish.returncode == 0, finish.stdout + finish.stderr
    proc = merge(stopped, "--fold-in", "--yes", *NO_R)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "- Add C 2020 c model." in (wt / "NEWS.md").read_text()


def test_finish_completes_a_fold_in_round_from_its_own_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """claude/fa and claude/fb append FOO at the same place in different
    words, so -X theirs keeps claude/fb's and the line check stops the round
    on claude/fa's Notes. Once the hand union is committed, --finish gates
    the round's merges against the tip it started from, and the PR text
    counts the whole branch."""
    repo = new_repo(tmp_path, monkeypatch, {"NEWS.md": NEWS, REGISTER: BASE_REGISTER})
    push_model_branch(repo, "a")
    first = merge(repo, "--yes", *NO_R)
    assert first.returncode == 0, first.stdout + first.stderr
    push_task_branch(repo, "claude/fa", {REGISTER: BASE_REGISTER + FOO_A}, "Add Fa")
    push_task_branch(repo, "claude/fb", {REGISTER: BASE_REGISTER + FOO_B}, "Add Fb")
    proc = merge(repo, "--fold-in", "--yes", *NO_R)
    assert proc.returncode == 9, proc.stdout + proc.stderr
    assert "    [claude/fa] - **Notes:** Alpha note.\n" in proc.stdout
    printed = shlex.split(proc.stderr.rstrip("\n").splitlines()[-1])
    assert printed[-2:] == ["--finish", "--hand-union-done"]
    wt = repo / ".worktrees" / BRANCH
    left = run(*printed)
    assert left.returncode == 9, left.stdout + left.stderr
    text = (wt / REGISTER).read_text()
    (wt / REGISTER).write_text(text.replace("the longer one.", "the longer one. Alpha note."))
    git(wt, "commit", "-qam", "Union the FOO notes by hand")
    done = run(*printed)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "\nMerge 3 claude/* branches (1 new models)\n" in done.stdout


def test_the_line_check_reads_only_the_rounds_branches(consolidated: Path) -> None:
    """claude/a and claude/b are on the round's base; only claude/c is checked."""
    proc = merge(consolidated, "--fold-in", "--yes", *NO_R)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    section = proc.stdout.split(
        "==> Checking that every line a merged branch added is in the result\n"
    )[1]
    assert section.startswith(
        f"    (lines) OK — every line a merged branch added to {REGISTER} is in the merge result\n"
        "    (lines) OK — every line a merged branch added to NEWS.md is in the merge result\n"
    )
    assert "claude/a" not in section.split("==>")[0]
