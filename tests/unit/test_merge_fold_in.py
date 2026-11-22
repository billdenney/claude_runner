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

import os
import shlex
import shutil
import subprocess
import sys
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


def merge(
    repo: Path, *flags: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return run("bash", MERGE_BRANCHES, "--repo", repo, "--branch-name", BRANCH, *flags, env=env)


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


def test_a_late_branch_renaming_a_file_onto_a_consolidated_path_stops_the_survey(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """claude/c renames Old.R onto the model claude/a added, with an edit:
    without rename detection that is an added path, and it collides."""
    old = "inst/modeldb/Old_2010_o.R"
    body = "".join(f"line {k}\n" for k in range(40))
    repo = new_repo(tmp_path, monkeypatch, {"NEWS.md": NEWS, REGISTER: BASE_REGISTER, old: body})
    push_model_branch(repo, "a")
    first = merge(repo, "--yes", *NO_R)
    assert first.returncode == 0, first.stdout + first.stderr
    git(repo, "checkout", "-q", "-b", "claude/c", "main")
    git(repo, "mv", old, "inst/modeldb/A_2020_a.R")
    (repo / "inst/modeldb/A_2020_a.R").write_text(body + "renamed\n")
    git(repo, "commit", "-qam", "Rename Old onto A 2020")
    git(repo, "push", "-q", "origin", "claude/c")
    git(repo, "checkout", "-q", "main")
    proc = merge(repo, "--fold-in", "--yes", *NO_R)
    assert proc.returncode == 4, proc.stdout + proc.stderr
    assert proc.stderr.startswith(
        "ERROR: origin/claude/c adds inst/modeldb/A_2020_a.R, which consolidation already has"
        " with other content.\n"
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
    # The report as the repairs left it, which the line check reads.
    repairs = path_in_git_dir(wt, "hand-union-report.repairs.md")
    held_by_repairs = repairs.read_text()
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
    assert (
        path_in_git_dir(wt, f"hand-union-report.repairs.before-{tip[:12]}.md").read_text()
        == held_by_repairs
    )
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


def records(wt: Path) -> dict[str, str]:
    """The round's records in the worktree's git directory, by name."""
    git_dir = path_in_git_dir(wt, "x").parent
    return {
        path.name: path.read_text()
        for path in sorted(git_dir.iterdir())
        if path.name.startswith(("merge-inputs", "merge-stages", "hand-union-report"))
    }


def fake_tool(tmp_path: Path, name: str, script: str) -> dict[str, str]:
    """The environment with ``script`` first on PATH as ``name``."""
    bin_dir = tmp_path / f"bin-{name}"
    bin_dir.mkdir()
    (bin_dir / name).write_text(script)
    (bin_dir / name).chmod(0o755)
    return {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}


def news_union_crashes(tmp_path: Path) -> dict[str, str]:
    """The environment in which union_merge_news.py crashes: the round stops
    after its merges, before its repairs complete."""
    return fake_tool(
        tmp_path,
        "python3",
        '#!/bin/bash\ncase "$1" in */union_merge_news.py) exit 3 ;; esac\n'
        f'exec {shlex.quote(sys.executable)} "$@"\n',
    )


def test_a_fold_in_whose_merges_all_fail_leaves_the_round_before_as_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """claude/c deletes a file claude/a changed: modify/delete, which -X
    theirs cannot resolve. Nothing merges, so the round before's records
    stay, and --finish and another --fold-in both still work."""
    repo = new_repo(
        tmp_path, monkeypatch, {"NEWS.md": NEWS, REGISTER: BASE_REGISTER, "inst/foo.txt": "x\n"}
    )
    push_task_branch(repo, "claude/a", {"inst/foo.txt": "y\n"}, "Change foo")
    first = merge(repo, "--yes", *NO_R)
    assert first.returncode == 0, first.stdout + first.stderr
    wt = repo / ".worktrees" / BRANCH
    tip, before = git(wt, "rev-parse", "HEAD").strip(), records(wt)
    git(repo, "checkout", "-q", "-b", "claude/c", "main")
    git(repo, "rm", "-q", "inst/foo.txt")
    git(repo, "commit", "-qm", "Delete foo")
    git(repo, "push", "-q", "origin", "claude/c")
    git(repo, "checkout", "-q", "main")

    proc = merge(repo, "--fold-in", "--yes", *NO_R)
    assert proc.returncode == 4, proc.stdout + proc.stderr
    assert proc.stderr == (
        "ERROR: no merges succeeded; nothing to push.\n"
        f"  The fold-in stopped while merging: {BRANCH} is back at {tip[:12]}, with the round"
        " before's records; run --fold-in again.\n"
    )
    assert (git(wt, "rev-parse", "HEAD").strip(), records(wt)) == (tip, before)
    assert git(wt, "status", "--porcelain") == ""

    finish = merge(repo, "--finish")
    assert finish.returncode == 0, finish.stdout + finish.stderr
    again = merge(repo, "--fold-in", "--yes", *NO_R)
    assert again.returncode == 4, again.stdout + again.stderr
    push_model_branch(repo, "d")
    other = merge(repo, "--fold-in", "--yes", "--exclude-ref", "origin/claude/c", *NO_R)
    assert other.returncode == 0, other.stdout + other.stderr
    assert "- Add D 2020 d model." in (wt / "NEWS.md").read_text()


def test_a_fold_in_interrupted_while_merging_is_put_back_at_its_tip(
    consolidated: Path, tmp_path: Path
) -> None:
    """A TERM while claude/c and claude/d merge: the round's merges go, the
    round before's records were never touched, and --fold-in runs again."""
    repo = consolidated
    push_model_branch(repo, "d")
    wt = repo / ".worktrees" / BRANCH
    tip, before = git(wt, "rev-parse", "HEAD").strip(), records(wt)
    count = tmp_path / "merges"
    real = shutil.which("git")
    assert real is not None
    env = fake_tool(
        tmp_path,
        "git",
        "#!/bin/bash\n"
        'if [[ "$1 $2" == "merge --no-ff" ]]; then\n'
        f"  echo x >> {shlex.quote(str(count))}\n"
        f'  if [[ "$(wc -l < {shlex.quote(str(count))})" -eq 2 ]]; then kill -TERM "$PPID"; fi\n'
        "fi\n"
        f'exec {shlex.quote(real)} "$@"\n',
    )
    proc = run(
        "bash",
        MERGE_BRANCHES,
        "--repo",
        repo,
        "--branch-name",
        BRANCH,
        "--fold-in",
        "--yes",
        *NO_R,
        env=env,
    )
    assert proc.returncode == 143, proc.stdout + proc.stderr
    assert proc.stderr.endswith(
        f"  The fold-in stopped while merging: {BRANCH} is back at {tip[:12]}, with the round"
        " before's records; run --fold-in again.\n"
    )
    assert (git(wt, "rev-parse", "HEAD").strip(), records(wt)) == (tip, before)
    again = merge(repo, "--fold-in", "--yes", *NO_R)
    assert again.returncode == 0, again.stdout + again.stderr
    news = (wt / "NEWS.md").read_text()
    assert "- Add C 2020 c model." in news and "- Add D 2020 d model." in news


def round_before_kept(wt: Path, tip: str) -> dict[str, str]:
    """The records kept as the round before's, under their own names."""
    return {
        name.replace(f".before-{tip[:12]}", ""): text
        for name, text in records(wt).items()
        if f".before-{tip[:12]}" in name
    }


def test_a_fold_in_killed_while_merging_is_undone_by_the_next(
    consolidated: Path, tmp_path: Path
) -> None:
    """A kill after claude/c merged runs no EXIT trap: the branch is left a
    merge ahead, with the round before's records. The marker the round left
    makes --finish refuse, and the next --fold-in put the branch back and
    fold in afresh."""
    repo = consolidated
    push_model_branch(repo, "d")
    wt = repo / ".worktrees" / BRANCH
    tip, before = git(wt, "rev-parse", "HEAD").strip(), records(wt)
    real = shutil.which("git")
    assert real is not None
    env = fake_tool(
        tmp_path,
        "git",
        "#!/bin/bash\n"
        f'{shlex.quote(real)} "$@"\n'
        "rc=$?\n"
        'if [[ "$1 $2" == "merge --no-ff" ]]; then kill -KILL "$PPID"; fi\n'
        'exit "$rc"\n',
    )
    killed = merge(repo, "--fold-in", "--yes", *NO_R, env=env)
    assert killed.returncode == -9, killed.stdout + killed.stderr
    assert git(wt, "rev-list", "--count", f"{tip}..HEAD").strip() != "0"
    assert records(wt) == before
    assert path_in_git_dir(wt, "fold-in-merging").read_text() == f"{tip}\n"

    finish = merge(repo, "--finish")
    assert (finish.returncode, finish.stderr) == (
        3,
        f"ERROR: --finish: the fold-in round from {tip[:12]} was killed while it merged, before"
        " its repairs, which --finish does not redo. Run --fold-in again: it puts"
        f" {BRANCH} back at that tip, with the round before's records, and folds in again.\n",
    )
    again = merge(repo, "--fold-in", "--yes", *NO_R)
    assert again.returncode == 0, again.stdout + again.stderr
    assert (
        f"    (fold-in) the round from {tip[:12]} was killed while it merged; {BRANCH}\n"
        f"    is back at {tip[:12]}, with the records of the round before it.\n"
        "    Its commits, still in the reflog:\n"
    ) in again.stdout
    merges = git(wt, "log", "--first-parent", "--format=%s", f"{tip}..HEAD")
    assert merges.count(f"Merge branch 'claude/c' into {BRANCH}") == 1
    assert merges.count(f"Merge branch 'claude/d' into {BRANCH}") == 1
    assert round_before_kept(wt, tip) == before
    assert not path_in_git_dir(wt, "fold-in-merging").exists()


def test_a_half_done_merge_a_killed_round_left_is_aborted(consolidated: Path) -> None:
    """Killed inside git merge: MERGE_HEAD and a staged index are left. The
    next --fold-in aborts that merge before it reads the worktree as dirty."""
    repo = consolidated
    wt = repo / ".worktrees" / BRANCH
    tip, before = git(wt, "rev-parse", "HEAD").strip(), records(wt)
    path_in_git_dir(wt, "fold-in-merging").write_text(f"{tip}\n")
    git(wt, "merge", "--no-ff", "--no-commit", "-X", "theirs", "origin/claude/c")
    assert git(wt, "status", "--porcelain") != ""

    again = merge(repo, "--fold-in", "--yes", *NO_R)
    assert again.returncode == 0, again.stdout + again.stderr
    assert (
        f"    (fold-in) the round from {tip[:12]} was killed while it merged; {BRANCH}\n"
        f"    is back at {tip[:12]}, with the records of the round before it.\n"
        "==> "
    ) in again.stdout
    assert "- Add C 2020 c model." in (wt / "NEWS.md").read_text()
    assert round_before_kept(wt, tip) == before


@pytest.mark.parametrize("stop", ["fails", "interrupted"])
def test_a_fold_in_that_stops_while_moving_the_records_puts_them_back(
    consolidated: Path, tmp_path: Path, stop: str
) -> None:
    """The round's merges landed and the round before's records are being
    moved aside when the move of merge-stages fails (the ERR trap) or an
    INT arrives: the branch goes back to its tip and so does every record."""
    repo = consolidated
    wt = repo / ".worktrees" / BRANCH
    tip, before = git(wt, "rev-parse", "HEAD").strip(), records(wt)
    real = shutil.which("mv")
    assert real is not None
    if stop == "fails":
        act = 'if [[ "$1" == */merge-stages ]]; then exit 1; fi\n'
    else:
        act = f'if [[ "$1" == */merge-stages ]]; then {shlex.quote(real)} "$@"; kill -INT "$PPID"; exit 0; fi\n'
    env = fake_tool(tmp_path, "mv", f'#!/bin/bash\n{act}exec {shlex.quote(real)} "$@"\n')
    proc = merge(repo, "--fold-in", "--yes", *NO_R, env=env)
    assert proc.returncode == (1 if stop == "fails" else 130), proc.stdout + proc.stderr
    assert proc.stderr.endswith(
        f"  The fold-in stopped while merging: {BRANCH} is back at {tip[:12]}, with the round"
        " before's records; run --fold-in again.\n"
    )
    assert (git(wt, "rev-parse", "HEAD").strip(), records(wt)) == (tip, before)
    assert not path_in_git_dir(wt, "fold-in-merging").exists()
    again = merge(repo, "--fold-in", "--yes", *NO_R)
    assert again.returncode == 0, again.stdout + again.stderr
    assert round_before_kept(wt, tip) == before


def test_a_fold_in_that_stops_before_its_repairs_is_started_again(
    consolidated: Path, tmp_path: Path
) -> None:
    """The NEWS union crashes after the round merged claude/c. --finish
    cannot complete a round whose repairs never ran, so it says to run
    --fold-in again, which puts the branch back at the tip, with the round
    before's records, and folds claude/c in afresh."""
    repo = consolidated
    wt = repo / ".worktrees" / BRANCH
    tip, before = git(wt, "rev-parse", "HEAD").strip(), records(wt)
    proc = merge(repo, "--fold-in", "--yes", *NO_R, env=news_union_crashes(tmp_path))
    assert proc.returncode == 5, proc.stdout + proc.stderr
    assert proc.stderr.endswith(
        "ERROR: union_merge_news.py failed on NEWS.md\n"
        "\n"
        "NOTE: this fold-in round stopped before its repairs completed. Discard any partial\n"
        f"  output in {wt}, then run --fold-in again: it puts {BRANCH} back at\n"
        f"  {tip[:12]}, with the round before's records, and folds in again.\n"
    )
    finish = merge(repo, "--finish")
    assert (finish.returncode, finish.stderr) == (
        3,
        f"ERROR: --finish: the fold-in round from {tip[:12]} stopped before its repairs"
        " completed, which --finish does not redo. Run --fold-in again: it puts"
        f" {BRANCH} back at that tip, with the round before's records, and folds in again.\n",
    )
    again = merge(repo, "--fold-in", "--yes", *NO_R)
    assert again.returncode == 0, again.stdout + again.stderr
    assert (
        f"    (fold-in) the round from {tip[:12]} stopped before its repairs completed; {BRANCH}\n"
    ) in again.stdout
    assert "- Add C 2020 c model." in (wt / "NEWS.md").read_text()
    merges = git(wt, "log", "--first-parent", "--format=%s", f"{tip}..HEAD")
    assert merges.count(f"Merge branch 'claude/c' into {BRANCH}") == 1
    assert round_before_kept(wt, tip) == before


def test_a_stopped_round_that_was_pushed_is_not_put_back_behind_origin(
    consolidated: Path, tmp_path: Path
) -> None:
    """The operator pushed a round that stopped before its repairs. Putting
    the branch back at the round's start would leave it behind origin, and
    its next push would be rejected, so --fold-in refuses and changes
    nothing."""
    repo = consolidated
    wt = repo / ".worktrees" / BRANCH
    tip = git(wt, "rev-parse", "HEAD").strip()
    proc = merge(repo, "--fold-in", "--yes", *NO_R, env=news_union_crashes(tmp_path))
    assert proc.returncode == 5, proc.stdout + proc.stderr
    git(wt, "push", "-q", "origin", BRANCH)
    pushed, before = git(wt, "rev-parse", "HEAD").strip(), records(wt)

    refused = merge(repo, "--fold-in", "--yes", *NO_R)
    assert (refused.returncode, refused.stderr) == (
        3,
        f"ERROR: --fold-in: the fold-in round from {tip[:12]} stopped before its repairs"
        f" completed, but origin/{BRANCH}, at {pushed[:12]}, has commits {tip[:12]} lacks: the"
        f" stopped round was pushed, or other work was. Putting {BRANCH} back at {tip[:12]}"
        " would leave it behind origin, so nothing was changed. Consolidate afresh on a new"
        f" --branch-name, or reconcile {BRANCH} with origin by hand.\n",
    )
    assert (git(wt, "rev-parse", "HEAD").strip(), records(wt)) == (pushed, before)


def test_a_stopped_round_is_put_back_when_origin_cannot_be_reached(
    consolidated: Path, tmp_path: Path
) -> None:
    """The fetch that would show a pushed round fails: the round is put back,
    against origin as last fetched, and the fold-in says so."""
    repo = consolidated
    wt = repo / ".worktrees" / BRANCH
    tip = git(wt, "rev-parse", "HEAD").strip()
    proc = merge(repo, "--fold-in", "--yes", *NO_R, env=news_union_crashes(tmp_path))
    assert proc.returncode == 5, proc.stdout + proc.stderr
    shutil.rmtree(tmp_path / "origin.git")

    again = merge(repo, "--fold-in", "--yes", *NO_R)
    assert again.returncode == 3, again.stdout + again.stderr
    assert again.stderr.startswith(
        f"    (fold-in) WARNING: cannot fetch {BRANCH} from origin; comparing with"
        f" origin/{BRANCH} as last fetched, if it was.\n"
    )
    assert f"    is back at {tip[:12]}, with the records of the round before it.\n" in again.stdout
    assert git(wt, "rev-parse", "HEAD").strip() == tip


def test_a_stopped_round_never_pushed_is_put_back_without_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Origin has no copy of a branch every round of which ran with
    --skip-push: nothing to fall behind, and nothing to warn of."""
    repo = new_repo(tmp_path, monkeypatch, {"NEWS.md": NEWS, REGISTER: BASE_REGISTER})
    push_model_branch(repo, "a")
    assert merge(repo, "--yes", "--skip-push", *NO_R).returncode == 0
    push_model_branch(repo, "c")
    wt = repo / ".worktrees" / BRANCH
    tip = git(wt, "rev-parse", "HEAD").strip()
    env = news_union_crashes(tmp_path)
    assert merge(repo, "--fold-in", "--yes", "--skip-push", *NO_R, env=env).returncode == 5

    again = merge(repo, "--fold-in", "--yes", "--skip-push", *NO_R)
    assert again.returncode == 0, again.stdout + again.stderr
    assert "WARNING" not in again.stderr
    assert f"    is back at {tip[:12]}, with the records of the round before it.\n" in again.stdout


def test_a_push_origin_rejects_as_not_a_fast_forward_says_so(consolidated: Path) -> None:
    """A commit pushed to the branch from elsewhere: the push is not the
    network's fault, and the message says what to do instead."""
    repo = consolidated
    git(repo, "checkout", "-q", "-b", "elsewhere", f"origin/{BRANCH}")
    git(repo, "commit", "-q", "--allow-empty", "-m", "Pushed from elsewhere")
    git(repo, "push", "-q", "origin", f"elsewhere:{BRANCH}")
    git(repo, "checkout", "-q", "main")
    wt = repo / ".worktrees" / BRANCH
    (wt / "notes.txt").write_text("a hand commit\n")
    git(wt, "add", "notes.txt")
    git(wt, "commit", "-qm", "A hand commit")

    proc = merge(repo, "--finish")
    assert proc.returncode == 7, proc.stdout + proc.stderr
    assert proc.stderr.endswith(
        f"ERROR: origin rejected the push as not a fast-forward: origin/{BRANCH} has\n"
        f"  commits {BRANCH} lacks, pushed since. Fetch and read them\n"
        f"  (git log {BRANCH}..origin/{BRANCH}), merge what belongs into {BRANCH}\n"
        f"  in {wt}, or consolidate afresh on a new --branch-name; then run\n"
        f"     bash {MERGE_BRANCHES} --repo {repo} --branch-name {BRANCH} --finish\n"
    )


def test_a_fold_in_needs_the_round_before_to_have_passed_its_revert_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fold-in round stops at its revert check: claude/c's hunk replaced
    claude/a's Notes sentence. Another fold-in on top would take the
    reverted register as its base, so it is refused until --finish has
    checked again. A union that drops claude/a's sentence still fails the
    revert check; the natural hand union, both sentences on one line, passes
    it and the line check, with no flag."""
    repo = new_repo(tmp_path, monkeypatch, {"NEWS.md": NEWS, REGISTER: BASE_REGISTER})
    push_model_branch(repo, "a", notes="Alpha sentence.")
    assert merge(repo, "--yes", *NO_R).returncode == 0
    push_model_branch(repo, "c", notes="Gamma sentence.")
    stopped = merge(repo, "--fold-in", "--yes", *NO_R)
    assert stopped.returncode == 5, stopped.stdout + stopped.stderr
    # claude/d leaves WT alone, so its merge cannot revert the Notes line again.
    news_d = NEWS.replace("\n\n", "\n\n- Add D 2020 d model.\n", 1)
    push_task_branch(
        repo, "claude/d", {"NEWS.md": news_d, "inst/modeldb/D_2020_d.R": "# d\n"}, "Add D"
    )
    refused = merge(repo, "--fold-in", "--yes", *NO_R)
    assert (refused.returncode, refused.stderr) == (
        3,
        "ERROR: --fold-in: the round before has not passed its revert check (it stopped at it,"
        " or before it). Restore what it reports and run --finish, which checks again, then run"
        " --fold-in.\n",
    )
    wt = repo / ".worktrees" / BRANCH
    assert "- **Notes:** Base note. Gamma sentence.\n" in (wt / REGISTER).read_text()
    git(wt, "commit", "-q", "--allow-empty", "-m", "A union that keeps only Gamma")
    still = merge(repo, "--finish")
    assert still.returncode == 5, still.stdout + still.stderr
    assert "      missing line: - **Notes:** Base note. Alpha sentence.\n" in still.stdout
    text = (wt / REGISTER).read_text()
    (wt / REGISTER).write_text(text.replace("Gamma sentence.", "Alpha sentence. Gamma sentence."))
    git(wt, "commit", "-qam", "Union the Notes sentences by hand")
    finish = merge(repo, "--finish")
    assert finish.returncode == 0, finish.stdout + finish.stderr
    assert "WARNING" not in finish.stdout
    done = merge(repo, "--fold-in", "--yes", *NO_R)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "- Add D 2020 d model." in (wt / "NEWS.md").read_text()
    assert (
        "- **Notes:** Base note. Alpha sentence. Gamma sentence.\n" in (wt / REGISTER).read_text()
    )


def test_a_commit_after_the_revert_check_passed_is_no_change_when_no_register_is_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no union file and no register files the revert check guards
    nothing, so a hand commit on the consolidation leaves nothing for
    --finish to check again before a fold-in."""
    repo = new_repo(tmp_path, monkeypatch, {"NEWS.md": NEWS, REGISTER: BASE_REGISTER})
    push_model_branch(repo, "a")
    first = merge(repo, "--yes", "--union-file", "", "--no-register-files", *NO_R)
    assert first.returncode == 0, first.stdout + first.stderr
    wt = repo / ".worktrees" / BRANCH
    (wt / "inst" / "unrelated.txt").write_text("x\n")
    git(wt, "add", "inst/unrelated.txt")
    git(wt, "commit", "-qm", "An unrelated hand commit")
    push_model_branch(repo, "c")
    folded = merge(repo, "--fold-in", "--yes", *NO_R)
    assert folded.returncode == 0, folded.stdout + folded.stderr
    assert "- Add C 2020 c model." in (wt / "NEWS.md").read_text()


def test_a_register_edited_after_the_revert_check_passed_needs_finish_first(
    consolidated: Path,
) -> None:
    repo = consolidated
    wt = repo / ".worktrees" / BRANCH
    text = (wt / REGISTER).read_text()
    (wt / REGISTER).write_text(text.replace("Base note.", "Edited note."))
    git(wt, "commit", "-qam", "Edit WT by hand")
    refused = merge(repo, "--fold-in", "--yes", *NO_R)
    assert refused.returncode == 3, refused.stdout + refused.stderr
    assert refused.stderr.startswith(
        f"ERROR: --fold-in: {REGISTER} changed after the round before's revert check passed at "
    )
    assert "Merge branch 'claude/c'" not in git(wt, "log", "--format=%s")
